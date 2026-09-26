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
import json
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
from lib import mediainfo, naming, nfo as nfo_mod
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


def _locate(config, kind, tmdb_id):
    """定位媒体所在目录 + 目标 NFO 文件名。

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
        if not cat_folder:
            return None, None, folder_name, cloud
        parent = f"{cloud}/{cat_folder}"
        folder_path = _resolve_folder(config, cloud, cat_folder, folder_name, f"{parent}/{folder_name}")
    finally:
        s.close()

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
    elif kind == "tv" and nfo_items:
        return nfo_items[0]
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
def _build_meta(config, kind, tmdb_id):
    """取 NFO 所需的完整元数据: TMDB 直连优先, 失败回退本地缓存行。

    返回的 meta 形状与 lib/nfo.build_*_nfo 完全对齐(与 organize.resolve_full_meta 同口径)。"""
    meta = None
    if (config.get("tmdb", {}) or {}).get("api_key"):
        try:
            meta = tmdb.detail_sync(config, kind, tmdb_id)
        except Exception:  # noqa: BLE001
            meta = None

    if not meta:
        # 回退: 本地 TmdbMedia 行(展示/分类口径同 organize)
        s = SessionLocal()
        try:
            m = repo.get_tmdb_media_by_id(s, kind, tmdb_id)
            if m:
                meta = {
                    "tmdb_id": m.tmdb_id, "imdb_id": m.imdb_id or "",
                    "tvdb_id": m.tvdb_id or None, "title": m.title or "",
                    "originalTitle": m.original_title or "", "year": m.year or "",
                    "vote": m.vote or 0, "vote_count": 0,
                    "certification": m.certification or "",
                    "genres": [g for g in (m.genre_names or "").split(",") if g],
                    "countries": [c for c in (m.countries or "").split(",") if c],
                    "languages": [m.original_language] if m.original_language else [],
                    "studios": _json_list(m.studios_json),
                    "cast": _json_list(m.cast_json),
                    "directors": _json_list(m.directors_json),
                    "producers": [], "keywords": _json_list(m.keywords_json),
                    "runtime": m.runtime or 0, "premiered": m.premiered or "",
                    "end_date": m.end_date or "", "status": m.status or "",
                    "overview": m.overview or "", "tagline": "",
                    "trailer": "", "collection": None, "seasons": [],
                    "english_title": m.english_title or "", "kind": m.kind,
                }
                if kind == "tv":
                    meta["seasons"] = [{"number": ss.season_number, "name": ss.name or ""}
                                       for ss in repo.get_tmdb_seasons(s, tmdb_id)]
        finally:
            s.close()

    if not meta:
        return None

    # 英文名(?language=en): TMM 的 <english_title> 取这个
    if not meta.get("english_title") and (config.get("tmdb", {}) or {}).get("api_key"):
        try:
            meta["english_title"] = tmdb.english_title_sync(config, kind, tmdb_id)
        except Exception:  # noqa: BLE001
            meta["english_title"] = ""

    # 剧集 seasons 兜底(本地表有但直连没带时)
    if kind == "tv" and not meta.get("seasons"):
        s = SessionLocal()
        try:
            meta["seasons"] = [{"number": ss.season_number, "name": ss.name or ""}
                               for ss in repo.get_tmdb_seasons(s, tmdb_id)]
        finally:
            s.close()
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


def _fileinfo(config, folder_path, nfo_name, video):
    """电影 NFO 的 <fileinfo> 段: 只【保留】现有 NFO 里已有的, 绝不重新探测媒体。

    设计依据: NFO「更新」刷新的是 TMDB 元数据(类型/分类/ID/演员导演), 而 <fileinfo>
    (分辨率/编码/音轨等流信息) 是媒体文件本身的物理属性, 首次整理时已探测写入,
    刷新元数据时应当原样沿用, 不该重新推导 —— 重新推导既无意义, 又会在读不到
    现有文件/无探测工具的环境里把流信息弄丢。

    因此只从【现有 NFO 文件】把 <fileinfo>...</fileinfo> 抽出来原样回填(本地挂载读 →
    WebDAV GET 读)。都读不到 → 返回 None(不生成新的 streamdetails)。
    若想给老 NFO 补 <fileinfo>, 应走整理(organize)重新探测, 而不是 NFO 更新。"""
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
        return {"exists": True, "updated_at": wt or "", "updated_at_text": _fmt_write_time(wt),
                "path": it.get("fullPathName") or (folder_path + "/" + name),
                "name": name, "note": ""}
    try:
        return await run_in_threadpool(_q)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        return {"exists": False, "updated_at": "", "updated_at_text": "",
                "path": "", "name": "", "note": "读取失败(CD2 不可达?)"}


@router.post("/nfo/update/{kind}/{tmdb_id}")
async def nfo_update(kind: str, tmdb_id: int, cfg: dict = Depends(get_config)):
    """手动重新生成 NFO 并写回 /Cloud(走 cd2.write_file 的中转+Overwrite, 不删任何东西)。

    刷新的是 TMDB 元数据(类型/分类/ID/演员导演); <fileinfo> 只【沿用现有 NFO】里
    已有的(本地挂载读 → WebDAV GET 读), 读不到则留空, 绝不重新探测媒体 —— 流信息是
    媒体物理属性, 应由整理(organize)负责, 不该在元数据刷新时改动。返回写入结果 + 新的更新时间。"""
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")

    def _q():
        folder_path, nfo_name, folder_name, _cloud = _locate(cfg, kind, tmdb_id)
        if not folder_path or not nfo_name:
            raise HTTPException(404, "找不到媒体所在目录(可能尚未整理入库)")

        items = _safe_subfiles(cfg, folder_path)
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

        meta = _build_meta(cfg, kind, tmdb_id)
        if not meta:
            raise HTTPException(502, "无法获取元数据(TMDB 不可达且本地无缓存)")

        # 非阻塞冲突提示: 更新端点 ID 已钉死(来自 URL/本地缓存),不存在整理侧"反查错配"
        # 的根因,故冲突时【不拒写】,仅记日志 + 在响应带 warning 字段提醒用户 TMDB 可能与库内不一致
        warning = ""
        ex_ids = _nfo_ids_from_text(_existing_nfo_text(cfg, folder_path, [nfo_name]))
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
        dateadded = time.time()

        if kind == "tv":
            xml = nfo_mod.build_tvshow_nfo(meta, wikidata=wikidata,
                                           dateadded=dateadded)
        else:
            streamdetails = _fileinfo(cfg, folder_path, nfo_name, video)
            source = mediainfo.guess_source(video.get("name")) if video else ""
            # <original_filename> 沿用现有 NFO 里的(整理写的是改名前原始发布名),
            # 读不到才回退当前视频名(新建 NFO 场景)
            orig = _original_filename(cfg, folder_path, nfo_name, video)
            if not orig:
                orig = video.get("name") if video else ""
            xml = nfo_mod.build_movie_nfo(meta, info=None, streamdetails=streamdetails,
                                          source=source, original_filename=orig,
                                          dateadded=dateadded, wikidata=wikidata)

        written = cd2.write_file(cfg, nfo_path, xml, base_dir=PROJECT_ROOT)
        it = cd2.find_file_by_path(cfg, folder_path, nfo_name)
        wt = it.get("writeTime") if it else ""
        return {"ok": True, "path": nfo_path, "name": nfo_name,
                "bytes": written, "updated_at": wt or "",
                "updated_at_text": _fmt_write_time(wt), "warning": warning}
    return await run_in_threadpool(_q)
