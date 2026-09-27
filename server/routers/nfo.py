"""NFO 更新路由
================================================================
需求: 库内媒体的 NFO 上次更新时间查询 + 手动重新生成 NFO。

两个端点:
  GET  /api/nfo/info/{kind}/{tmdb_id}   读取库内 NFO 的 writeTime(上次更新时间)
  POST /api/nfo/update/{kind}/{tmdb_id} 用最新元数据重建 NFO 并写回 /Cloud(走 cd2.write_file 的中转+Overwrite, 不破坏 /Cloud 禁删铁律)

落点规则与整理(scripts/organize.py)完全一致:
  - 目录 = cloud_root/<分类目录>/<标题 (年份)>
  - 电影 NFO = <视频文件名去扩展名>.nfo; 剧集 NFO = tvshow.nfo
NFO 格式对齐 TMM 5.2.12(JELLYFIN 口径), 见 lib/nfo.py。
"""
import os
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from server.auth import require_auth
from server.config import PROJECT_ROOT, get_config
from db.database import SessionLocal
from db import repositories as repo
from clients.clouddrive import client as cd2
from clients.tmdb import client as tmdb
from lib import mediainfo, naming, nfo as nfo_mod, titles
from scripts import organize

router = APIRouter(prefix="/api", tags=["nfo"], dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# 工具: 元数据 / 目录定位 / 文件信息
# ---------------------------------------------------------------------------
def _json_list(text):
    """把 JSON 文本安全解析成列表(空/异常返回 [])。"""
    if not text:
        return []
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _classify_meta(m):
    """把 TmdbMedia 行摊平成 lib/classify 需要的 media dict(分类用)。"""
    return {
        "title": m.title or "",
        "kind": m.kind,
        "genres": [int(x) for x in (m.genres or "").split(",") if x.strip().isdigit()],
        "original_language": m.original_language or "",
        "countries": [c for c in (m.countries or "").split(",") if c],
        "certification": (m.certification or "").strip(),
        "adult": False,
    }


def _safe_subfiles(config, path):
    """get_subfiles 包一层 try(CD2 不可达时返回空, 不让接口崩)。"""
    try:
        return cd2.get_subfiles(config, path) or []
    except Exception:  # noqa: BLE001
        return []


def _largest_video(items):
    """从目录文件列表里挑最大的视频文件(主媒体文件)。"""
    best = None
    for it in items:
        if cd2.is_dir(it):
            continue
        nm = it.get("name") or ""
        ext = os.path.splitext(nm)[1].lower()
        if ext in naming.VIDEO_EXT:
            if best is None or int(it.get("size") or 0) > int(best.get("size") or 0):
                best = it
    return best


def _resolve_folder(config, cloud, cat_folder, folder_name, expected):
    """定位分类目录下的作品文件夹。

    首选精确匹配; 次选: 归一化(sanitize)同名子目录(防御 TMM/人工对目录名做的小幅改动);
    都找不到则退回期望路径(让后续查找返回 exists=False, 而不是 404)。"""
    parent = f"{cloud}/{cat_folder}"
    items = _safe_subfiles(config, parent)
    target_norm = naming.sanitize(folder_name)
    for it in items:
        if cd2.is_dir(it) and naming.sanitize(it.get("name") or "") == target_norm:
            return it.get("fullPathName") or f"{parent}/{it.get('name')}"
    return expected


def _dir_exists(config, path):
    """CD2 里这个目录存在且非空?

    **只有"确知不存在"才返回 False**; 列目录超时 / 网络抖动这类"未知"必须往上抛,
    否则 _locate 会把"查不到"当成"没有", 退到分类目录里再找一个同名文件夹 ——
    定位到错误目录后照样写 NFO(2026-09-26 审查)。
    """
    if not path:
        return False
    try:
        return bool(cd2.get_subfiles(config, path) or [])
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if any(k in msg for k in ("not found", "not_found", "no such", "不存在")):
            return False
        raise HTTPException(502,
                            f"CD2 列目录失败,无法确认目录是否存在(拒绝退到分类目录): "
                            f"{path} — {str(e)[:160]}") from e


def _jf_folder_candidates(jf_path, kind, cloud):
    """Jellyfin 的 Path → CD2 目录候选(按可信度排序, 调用方逐个验存在)。

    本部署两边根不一致: Jellyfin 看到 /Cloud/…, CD2 是 /115/Cloud/…。
    所以除了原样, 还把路径开头逐段去掉再接到 cloud_root 后面试。
    """
    p = (jf_path or "").replace("\\", "/").strip().rstrip("/")
    if not p:
        return []
    if kind == "movie":
        p = os.path.dirname(p)   # 电影的 Path 是文件
    if not p:
        return []
    cloud = (cloud or "").rstrip("/")
    out = []
    if cloud and (p == cloud or p.startswith(cloud + "/")):
        out.append(p)                      # ① 原样(两边根一致时直接命中)
    parts = [x for x in p.split("/") if x]
    if cloud:
        for i in (1, 2):                   # ② 丢掉开头 1~2 段: /Cloud/CnShow/X → <cloud>/CnShow/X
            if i >= len(parts):
                break
            cand = f"{cloud}/{'/'.join(parts[i:])}"
            if cand not in out:
                out.append(cand)
    return out


def _locate(config, kind, tmdb_id):
    """定位媒体所在目录 + 目标 NFO 文件名。

    顺序:
      ① jellyfin_item 镜像里 **Jellyfin 的真实 Path**(权威)—— 分类规则把港剧推成
         HkShow 而库里实际在 CnShow、或目录被人改过名, 都以 Jellyfin 为准;
      ② 回退: TMDB 元数据 → 分类规则 → 在分类目录里找同名子目录(与整理口径一致)。

    返回 (folder_path, nfo_name, folder_name, cloud)。
    找不到本地缓存行 → 全返回 None; 文件夹无法解析 → folder_path 退回期望路径(后续 exists=False)。"""
    s = SessionLocal()
    try:
        m = repo.get_tmdb_media_by_id(s, kind, tmdb_id)
        if not m:
            return None, None, None, None
        folder_name = naming.folder_name_from_meta({"title": m.title or "", "year": m.year or ""})
        cat_folder, _key, _reasons = organize.category_of(config, _classify_meta(m), folder_name, [])
        cloud = organize.cloud_root(config)
        jf_path = repo.get_jellyfin_item_path(s, kind, tmdb_id)
    finally:
        s.close()

    folder_path = ""
    for cand in _jf_folder_candidates(jf_path, kind, cloud):
        if _dir_exists(config, cand):
            folder_path = cand
            break

    if not folder_path:
        if not cat_folder:
            return None, None, folder_name, cloud
        parent = f"{cloud}/{cat_folder}"
        folder_path = _resolve_folder(config, cloud, cat_folder, folder_name, f"{parent}/{folder_name}")

    if kind == "tv":
        nfo_name = "tvshow.nfo"
    else:
        nfo_name = (folder_name or "movie") + ".nfo"
    return folder_path, nfo_name, folder_name, cloud


def _find_nfo_file(config, folder_path, kind, nfo_name):
    """在作品文件夹里找 NFO 文件(返回 CloudDriveFile dict 或 None)。

    电影: 优先与最大视频文件同基名的 .nfo, 否则任意 .nfo;
    剧集: 优先 tvshow.nfo, 否则任意 .nfo。"""
    items = _safe_subfiles(config, folder_path)
    # 首选精确匹配
    for it in items:
        if (not cd2.is_dir(it)) and (it.get("name") or "") == nfo_name:
            return it
    nfo_items = [it for it in items
                 if (not cd2.is_dir(it)) and (it.get("name") or "").lower().endswith(".nfo")]
    if kind == "movie":
        video = _largest_video(items)
        if video:
            base = os.path.splitext(video.get("name"))[0]
            for it in nfo_items:
                if os.path.splitext(it.get("name"))[0] == base:
                    return it
        if nfo_items:
            return nfo_items[0]
    # 剧集【只认】tvshow.nfo, 不做"任意 .nfo"兜底: 目录里可能有下载包自带的集级
    # S01E01.nfo(别家刮的), 拿它兜底会把整集刮削结果覆盖成剧集元数据。
    # 找不到就返回 None → 由调用方用默认名 tvshow.nfo 新建。
    return None


def _fmt_write_time(wt):
    """CD2 的 writeTime 是 RFC3339 字符串(如 2023-01-01T12:00:00Z) → 'YYYY-MM-DD HH:MM:SS'。"""
    if not wt:
        return ""
    s = str(wt).strip()
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return s.replace("T", " ").replace("Z", "")


# ---------------------------------------------------------------------------
# 元数据构建(更新 NFO 用)
# ---------------------------------------------------------------------------
def _build_meta(config, kind, tmdb_id, *, title_read_only: bool = False):
    """取 NFO 所需的完整元数据: **只走 TMDB 直连, 没有任何本地回退**。

    铁律(用户要求): NFO 生成不许读本地缓存行 —— TMDB 取不到就让「更新 NFO」直接
    报错失败, 宁可不写, 也不把可能过期的本地数据写进 /Cloud。
    返回的 meta 形状与 lib/nfo.build_*_nfo 完全对齐(与 organize.resolve_full_meta 同口径)。
    失败抛 HTTPException 502(可读原因)。"""
    if not (config.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(502, "未配置 TMDB api_key — 无法直连生成 NFO(不回退本地缓存)")
    try:
        meta = tmdb.detail_sync(config, kind, tmdb_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            502, f"TMDB 直连获取元数据失败(不回退本地缓存): {str(e)[:200]}") from e
    if not meta:
        raise HTTPException(
            502, f"TMDB 直连未返回数据(kind={kind}, tmdb_id={tmdb_id}) — 不写 NFO")

    # 英文名(?language=en): TMM 的 <english_title> 取这个
    if not meta.get("english_title") and (config.get("tmdb", {}) or {}).get("api_key"):
        try:
            meta["english_title"] = tmdb.english_title_sync(config, kind, tmdb_id)
        except Exception:  # noqa: BLE001
            meta["english_title"] = ""

    # 中文标题兜底(手动覆盖 > 豆瓣国内译名 > TMDB 台/港译名)。
    # 直连 TMDB 拿到的 title 可能是英文, 且会绕过本地行,不补一次「更新 NFO」会把
    # 英文标题写回库里(2026-09-26 实测 Bad Sisters)。
    # 只动 title 一个键, 其余元数据仍原样来自 TMDB 直连。
    # 包 try: 标题兜底里的 DB 写入抖动不该把整个「更新 NFO」变成 500(与 organize 同口径)。
    try:
        titles.apply_to_meta(config, kind, tmdb_id, meta, read_only=title_read_only)
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠ 中文标题兜底失败(沿用 TMDB 直连 title): {str(e)[:120]}")
    return meta


def _existing_nfo_text(config, folder_path, names):
    """读【现有 NFO 文件】的纯文本(只读, 不探测媒体)。

    通道: 本地挂载读 → WebDAV GET 读。都读不到(文件当前不可读)返回 None。
    这是 NFO 更新时抽取"文件属性类"字段(<fileinfo> / <original_filename>)的唯一来源 ——
    更新只刷新 TMDB 元数据, 这些随文件走的属性应当原样沿用。"""
    for nm in names:
        lp = mediainfo.local_path(folder_path.rstrip("/") + "/" + nm, config)
        if lp and os.path.exists(lp):
            try:
                with open(lp, encoding="utf-8") as f:
                    return f.read()
            except Exception:  # noqa: BLE001
                pass
    if mediainfo.webdav_conf(config).get("base"):
        for nm in names:
            cd2_path = folder_path.rstrip("/") + "/" + nm
            try:
                txt = mediainfo.webdav_get_text(cd2_path, config)
                if txt:
                    return txt
            except Exception:  # noqa: BLE001
                pass
    return None


def _nfo_ids_from_text(txt):
    """从 NFO 文本解析已有 IDs(imdb/tmdb/tvdb),用于更新前的【非阻塞】冲突提示。

    与 organize._nfo_existing_ids 口径一致(取 uniqueid/imdbid/tmdbid),但只做提示用:
    更新端点 ID 已钉死(来自 URL/本地缓存),不存在整理侧"反查错配"的根因,所以冲突时
    不拒写,只返回 warning 字段 + 记日志,让用户知道 TMDB 数据可能与库内不一致。"""
    ids = {}
    if not txt:
        return ids
    for m in re.finditer(r'<uniqueid[^>]*type="(imdb|tmdb|tvdb|wikidata)"[^>]*>(.*?)</uniqueid>',
                         txt, re.S | re.I):
        ids[m.group(1).lower()] = m.group(2).strip().lower()
    for tag, key in (("imdbid", "imdb"), ("tmdbid", "tmdb")):
        mm = re.search(r'<%s>(.*?)</%s>' % (tag, tag), txt, re.S | re.I)
        if mm and key not in ids:
            ids[key] = mm.group(1).strip().lower()
    return ids


def _fileinfo_state(config, folder_path, nfo_name, video):
    """现有 NFO 的 <fileinfo> 三态: 'ok' / 'empty'(读到了但没有流信息) / 'unreadable'。

    **必须**区分 empty 与 unreadable: 两者都表现为"抽不出 <fileinfo>", 但后果完全不同 ——
    empty 是"这条没探测过"(该补), unreadable 是"读不到现有文件"(此时若照常重建,
    会把库里已有的 <fileinfo>/<original_filename>/<source> 抹成空标签)。
    读通道: 本地挂载 → WebDAV GET, 都不通则 unreadable。"""
    names = []
    if video:
        names.append(os.path.splitext(video.get("name"))[0] + ".nfo")
    names.append(nfo_name)
    txt = _existing_nfo_text(config, folder_path, names)
    if txt is None:
        return "unreadable"
    mm = re.search(r"<fileinfo>.*?</fileinfo>", txt, re.S)
    if mm and re.search(r"<streamdetails>", mm.group(0)):
        return "ok"
    return "empty"


def _fileinfo(config, folder_path, nfo_name, video):
    """电影 NFO 的 <fileinfo> 段: 只【保留】现有 NFO 里已有的, 绝不重新探测媒体。

    设计依据: NFO「更新」刷新的是 TMDB 元数据(类型/分类/ID/演员导演), 而 <fileinfo>
    (分辨率/编码/音轨等流信息) 是媒体文件本身的物理属性, 首次整理时已探测写入,
    刷新元数据时应当原样沿用, 不该重新推导 —— 重新推导既无意义, 又会在读不到
    现有文件/无探测工具的环境里把流信息弄丢。

    因此只从【现有 NFO 文件】把 <fileinfo>...</fileinfo> 抽出来原样回填(本地挂载读 →
    WebDAV GET 读)。读不到 → 返回 None —— **调用方必须先用 _fileinfo_state 区分
    "读不到"与"没有"**, 读不到时禁止重建(见 _rebuild_nfo 的闸门)。
    若想给老 NFO 补 <fileinfo>, 应走整理(organize)重新探测或 probe=1, 而不是裸更新。"""
    names = []
    if video:
        names.append(os.path.splitext(video.get("name"))[0] + ".nfo")
    names.append(nfo_name)
    txt = _existing_nfo_text(config, folder_path, names)
    if txt:
        mm = re.search(r"<fileinfo>.*?</fileinfo>", txt, re.S)
        if mm:
            return mm.group(0)
    return None


def _original_filename(config, folder_path, nfo_name, video):
    """电影 NFO 的 <original_filename>: 只【保留】现有 NFO 里已有的(与整理一致 ——
    整理写的是改名前的原始发布名)。读不到则回退当前视频名(新建 NFO 时)。

    不能用"当前视频名"硬填: 库内视频已被 TMM 改名(如 '1921 (2021) 1080p AC3.mkv'),
    那不是"原始文件名"。所以优先沿用现有 NFO 里 organize 写下的那份。"""
    names = []
    if video:
        names.append(os.path.splitext(video.get("name"))[0] + ".nfo")
    names.append(nfo_name)
    txt = _existing_nfo_text(config, folder_path, names)
    if txt:
        mm = re.search(r"<original_filename>(.*?)</original_filename>", txt, re.S)
        if mm:
            return mm.group(1).strip()
    return None


def _probe_fileinfo(config, folder_path, video):
    """现场探测一次媒体文件, 返回 (<fileinfo> XML 片段, 原因)。

    只给【现有 NFO 没有 <fileinfo> 的存量条目】用 —— 整理时 WebDAV 通常只开 /Temp,
    入库后想补流信息就只能靠 local_root 挂载或 WebDAV 覆盖到 /Cloud。
    读不到返回 (None, 可读原因), 调用方原样带回给前端, 不写空标签也写不出假数据。
    """
    if not video or not video.get("name"):
        return None, "目录里没有视频文件"
    timeout = mediainfo.probe_timeout(config)   # 0/负数/乱码 → 180, 上限 600, 免得坏配置让两条通道全秒失败
    notes = []

    def _log(msg):
        notes.append(str(msg).strip().lstrip("⚠").strip())

    path = folder_path.rstrip("/") + "/" + video["name"]
    try:
        info = mediainfo.probe(path, config, timeout=timeout, log=_log)
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:200]
    if not info:
        return None, (" / ".join(n for n in notes if n)[:300] or "本地挂载与 WebDAV 都读不到该文件")
    return mediainfo.streamdetails_xml(info, indent="  "), ""


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/nfo/info/{kind}/{tmdb_id}")
async def nfo_info(kind: str, tmdb_id: int, cfg: dict = Depends(get_config)):
    """读取库内 NFO 的上次更新时间(writeTime)。

    返回 {exists, updated_at(RFC3339), updated_at_text(YYYY-MM-DD HH:MM:SS),
          path, name}。CD2 不可达 / 找不到 NFO → exists=false(不影响详情页其余内容)。"""
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")

    def _q():
        folder_path, nfo_name, _folder_name, _cloud = _locate(cfg, kind, tmdb_id)
        if not folder_path or not nfo_name:
            return {"exists": False, "updated_at": "", "updated_at_text": "",
                    "path": "", "name": "", "note": "本地缓存无此条目"}
        it = _find_nfo_file(cfg, folder_path, kind, nfo_name)
        if not it:
            return {"exists": False, "updated_at": "", "updated_at_text": "",
                    "path": folder_path + "/" + nfo_name, "name": nfo_name,
                    "note": "尚未生成 NFO"}
        wt = it.get("writeTime")
        name = it.get("name") or nfo_name
        # has_fileinfo: 电影的 <fileinfo> 状态 —— 前端据此决定「更新 NFO」要不要带 probe=1。
        # 三态: true=有 / false=确知为空(才触发探测) / null=读不到现有 NFO(**不**触发,
        # 免得每次都跑一遍注定失败的探测; 更新时会被 502 闸门拦下, 见 _rebuild_nfo)。
        # 剧集 tvshow.nfo 本来就不写 fileinfo → 恒 true 不触发。
        has_fileinfo = True
        if kind == "movie":
            video = _largest_video(_safe_subfiles(cfg, folder_path))
            has_fileinfo = {"ok": True, "empty": False, "unreadable": None}.get(
                _fileinfo_state(cfg, folder_path, name, video), None)
        return {"exists": True, "updated_at": wt or "", "updated_at_text": _fmt_write_time(wt),
                "path": it.get("fullPathName") or (folder_path + "/" + name),
                "name": name, "note": "", "has_fileinfo": has_fileinfo}
    try:
        return await run_in_threadpool(_q)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        return {"exists": False, "updated_at": "", "updated_at_text": "",
                "path": "", "name": "", "note": "读取失败(CD2 不可达?)"}


@router.post("/nfo/update/{kind}/{tmdb_id}")
async def nfo_update(kind: str, tmdb_id: int, probe: bool = False,
                     cfg: dict = Depends(get_config)):
    """手动重新生成 NFO 并写回 /Cloud(走 cd2.write_file 的中转+Overwrite, 不删任何东西)。

    刷新的是 TMDB 元数据(类型/分类/ID/演员导演); <fileinfo> 默认【沿用现有 NFO】里
    已有的(本地挂载读 → WebDAV GET 读), 读不到则留空, 绝不擅自改写已有的流信息 ——
    流信息是媒体物理属性, 应由整理(organize)负责。probe=1 时额外一次机会: 现有 NFO
    压根没有 <fileinfo> 的存量条目, 现场探测一次补上(读不到照旧留空, 并带回原因)。
    返回写入结果 + 新的更新时间 + {probed, probe_error}。"""
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")

    def _q():
        folder_path, nfo_name, folder_name, _cloud = _locate(cfg, kind, tmdb_id)
        if not folder_path or not nfo_name:
            raise HTTPException(404, "找不到媒体所在目录(可能尚未整理入库)")
        return _rebuild_nfo(cfg, kind, tmdb_id, folder_path, nfo_name, folder_name,
                            probe=probe)

    return await run_in_threadpool(_q)


def _rebuild_nfo(cfg, kind, tmdb_id, folder_path, nfo_name, folder_name, probe=False):
    """生成 NFO 并写回 folder_path(目录必须已存在), 返回写入结果。

    「更新 NFO」与「按新标题重命名」共用;失败抛 HTTPException。
    目录不存在/不可达给可读 404, 而不是让 CD2 的 NOT_FOUND 炸成 500 堆栈。

    probe=True(仅电影): 现有 NFO 没有 <fileinfo> 时现场探测一次补上 —— 存量条目
    整理时没探到(容器缺 ffprobe / WebDAV 只开 /Temp)就靠这个补。已有 <fileinfo> 一律
    沿用不动(与整理一致: 流信息是物理属性, 不在元数据刷新时改写)。
    """

    try:
        items = cd2.get_subfiles(cfg, folder_path) or []
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"CD2 找不到媒体目录: {folder_path} — {e}") from e
    if not items:
        raise HTTPException(404, f"媒体目录为空或不可达: {folder_path}")

    video = _largest_video(items) if kind == "movie" else None
    # 优先沿用【已存在】的 NFO 文件名(电影可能与视频不同基名 / 剧集固定 tvshow.nfo),
    # 避免 CD2 列目录抖动时在「读时找不到 → 写时用默认名」之间产生第二个 NFO。
    existing = _find_nfo_file(cfg, folder_path, kind, nfo_name)
    if existing:
        nfo_name = existing.get("name") or nfo_name
    else:
        if kind == "movie":
            nfo_name = (os.path.splitext(video.get("name"))[0] + ".nfo") if video else (folder_name or "movie") + ".nfo"
        else:
            nfo_name = nfo_name  # tvshow.nfo
    nfo_path = folder_path.rstrip("/") + "/" + nfo_name

    # 闸门: NFO 文件**在**(列目录看到了)但内容**读不到** → 拒绝重建。
    # 否则 _fileinfo/_original_filename 会因为"读不到"返回 None, 把库里已有的
    # <fileinfo>/<original_filename>/<source> 一起抹成空标签(静默丢数据)。
    # 读通道只有两条: 本地挂载(local_root) / WebDAV GET(需 account_root 覆盖该目录)。
    # 读一次现有 NFO 文本, 后面闸门 / ID 校验 / 本机状态保留都用它(避免列目录抖动时
    # 三次读取拿到三种结果)。
    existing_text = _existing_nfo_text(cfg, folder_path, [nfo_name]) if existing else None
    if existing and existing_text is None:
        raise HTTPException(
            502, f"读不到现有 NFO 的内容({nfo_name}): 本地挂载与 WebDAV 都取不到 —— "
                 "为避免抹掉 <fileinfo>/<original_filename>/<source>, 已拒绝覆盖。"
                 "请把 webdav.account_root 改成 '/' 或给容器挂上媒体目录后重试")

    meta = _build_meta(cfg, kind, tmdb_id)
    if not meta:
        raise HTTPException(502, "无法获取元数据(TMDB 直连未返回, 不回退本地缓存)")

    # 非阻塞冲突提示: 更新端点 ID 已钉死(来自 URL/本地缓存),不存在整理侧"反查错配"
    # 的根因,故冲突时【不拒写】,仅记日志 + 在响应带 warning 字段提醒用户 TMDB 可能与库内不一致
    warning = ""
    ex_ids = _nfo_ids_from_text(existing_text or "")
    new_imdb = (meta.get("imdb_id") or "").strip().lower()
    new_tmdb = str(meta.get("tmdb_id") or "").lower()
    if ex_ids:
        if new_imdb and ex_ids.get("imdb") and new_imdb != ex_ids["imdb"]:
            warning = f"imdb 不一致: 库内 {ex_ids['imdb']} → 新 {new_imdb}（已按最新元数据写入）"
        elif new_tmdb and ex_ids.get("tmdb") and new_tmdb != ex_ids["tmdb"]:
            warning = f"tmdb 不一致: 库内 {ex_ids['tmdb']} → 新 {new_tmdb}（已按最新元数据写入）"
    if warning:
        print(f"    ⚠ NFO 更新 {warning}")

    wikidata = ""
    if organize.org_cfg(cfg).get("wikidata", True):
        cache = os.path.join(PROJECT_ROOT, "state", "wikidata_cache.json")
        wikidata = nfo_mod.wikidata_id(meta.get("imdb_id"), cache_file=cache)
    # 本机状态(dateadded / 观看记录): 从现有 NFO 读回, 刷新元数据时**原样保留** ——
    # 老代码每次更新都写 time.time(), 结果"加入时间"变成今天、观看状态被清零。
    state = nfo_mod.read_local_state(existing_text or "")
    dateadded = state.get("dateadded") or time.time()
    probed, probe_error, probe_attempted = False, "", False
    has_fileinfo = True          # 剧集 tvshow.nfo 不写 <fileinfo> → 恒 true(见 nfo_info)

    if kind == "tv":
        xml = nfo_mod.build_tvshow_nfo(meta, wikidata=wikidata,
                                       dateadded=dateadded,
                                       watched=state.get("watched") or None,
                                       playcount=state.get("playcount") or None,
                                       lastplayed=state.get("lastplayed") or None)
    else:
        streamdetails = _fileinfo(cfg, folder_path, nfo_name, video)
        if probe and not streamdetails:
            probe_attempted = True    # 真的跑了一次探测(probed=false 才是失败)
            streamdetails, probe_error = _probe_fileinfo(cfg, folder_path, video)
            probed = bool(streamdetails)
            if probed:
                print(f"    ✓ NFO 存量补探测成功: {nfo_name} ({len(streamdetails)} 字节 <fileinfo>)")
            else:
                print(f"    ⚠ NFO 存量补探测失败: {probe_error}")
        # <original_filename> 沿用现有 NFO 里的(整理写的是改名前原始发布名),
        # 读不到才回退当前视频名(新建 NFO 场景)
        orig = _original_filename(cfg, folder_path, nfo_name, video)
        if not orig:
            orig = video.get("name") if video else ""
        # 片源用【改名前】的原始发布名猜: 当前视频名已是 '标题 (年份) 2160p h265',
        # UHD.BluRay 这类字样丢了 → 会猜成 NONE
        source = mediainfo.guess_source(orig) if orig else ""
        xml = nfo_mod.build_movie_nfo(meta, info=None, streamdetails=streamdetails,
                                      source=source, original_filename=orig,
                                      dateadded=dateadded, wikidata=wikidata,
                                      watched=state.get("watched") or None,
                                      playcount=state.get("playcount") or None,
                                      lastplayed=state.get("lastplayed") or None)

    if kind != "tv":
        # 写完后的 <fileinfo> 状态: 前端据此判断"已有流信息、不用再提示探测失败"
        # —— probed=false 曾把"本来就有 / 没必要探"和"探了失败"混成一个信号(审查 P2)。
        has_fileinfo = bool(streamdetails)

    written = cd2.write_file(cfg, nfo_path, xml, base_dir=PROJECT_ROOT)
    # 取 writeTime 只是给前端显示"上次更新时间"; 列目录抖一下不该让**已成功写入**
    # 的整单变成失败(2026-09-26 审查) → 取不到就返回空, 下次 info 会重新读到。
    try:
        it = cd2.find_file_by_path(cfg, folder_path, nfo_name)
        wt = it.get("writeTime") if it else ""
    except Exception as e:  # noqa: BLE001
        wt = ""
        warning = (warning + " | " if warning else "") + f"写入成功,但读取更新时间失败: {str(e)[:120]}"
    return {"ok": True, "path": nfo_path, "name": nfo_name,
            "bytes": written, "updated_at": wt or "",
            "updated_at_text": _fmt_write_time(wt), "warning": warning,
            "probed": probed, "probe_error": probe_error,
            "probe_attempted": probe_attempted, "has_fileinfo": has_fileinfo}
