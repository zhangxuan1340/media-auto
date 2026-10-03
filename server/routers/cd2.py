"""CloudDrive2 路由: 推送离线下载 + 文件整理(扫描/执行/刮削刷新) + 分类规则管理

注意 CD2 多链接分隔铁律: 每个链接单独一次 AddOfflineFiles 调用(见 clients/clouddrive/client.py)。

整理执行(/organize/apply)是【后台任务】: 单条要 删广告→改名→WebDAV 探测(~5s)→写 NFO→移动,
"执行全部"可能跑几分钟。接口立即返回 job_id, 前端轮询 GET /organize/apply/{job_id} 取进度/结果。
"""
import re
import sys
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from server.auth import require_auth
from server.config import get_config, save_config, skill_root
from db.database import SessionLocal
from db import repositories as repo
from lib.classify import (FALLBACK_REGION, normalize_regions,
                          validate_regions)

router = APIRouter(prefix="/api", tags=["cd2"], dependencies=[Depends(require_auth)])

# ---- 分类规则(管理页「分类规则」子页签) ----
# 分类键两类:
#   * 特殊类型键(Dm/Jl/Xr/Sp/Mu 前缀)由级联引擎固定生成 —— 键不可增删, 只有目录名可改;
#   * 地区键 = <地区档键>Movie / <地区档键>Show, **随「地区档」配置变**:
#     档可以在页面上增/删/改(如把台湾拆成单独的 Tw 档), 键由地区档现算, 白名单跟着变。
# 这里维护"键 → 中文名 + 分组 + 说明"(地区键的中文名由档 label 现算), 前端按分组渲染,
# 后端校验键白名单。
_SPECIAL_META = {
    # (中文名, 分组, 说明)
    # ⚠️ 2026-09-22 移除 TsMovie/TsShow(18+ 不再按目录隔离, 改由 Jellyfin 按分级控制)。
    "DmMovie":  ("动画电影",   "special", "TMDB 动画类型 或 标题命中动画关键词"),
    "DmShow":   ("动画剧集",   "special", "TMDB 动画类型 或 标题命中动画关键词"),
    "JlShow":   ("纪录片",     "special", "纪录片统一进这里(不分电影/剧集, 含单部纪录电影)"),
    "XrShow":   ("综艺",       "special", "标题命中综艺/脱口秀/真人秀等关键词"),
    "SpShow":   ("体育",       "special", "标题命中体育/赛事/电竞等关键词"),
    "MuShow":   ("音乐",       "special", "标题命中演唱会/MV/专辑等关键词"),
}
# 目录名白名单: 非空, 不允许路径分隔符/控制字符, 不超长
_DIR_NAME_BAD = re.compile(r"[/\\\x00-\x1f]")


def _region_desc(key, it):
    """地区档 → 分类规则页的说明列(跟着配置走, 改了归属说明也跟着变)。"""
    bits = []
    prio = it.get("prio") or {}
    if prio:
        bits.append("优先 " + "/".join(
            f"{c}={'/'.join(v) or '不限'}" for c, v in prio.items()))
    if it.get("keywords"):
        bits.append("关键词命中优先")
    langs = list(it.get("languages") or [])
    if langs:
        bits.append("语言 " + " ".join(langs[:6]) + ("…" if len(langs) > 6 else ""))
    cts = list(it.get("countries") or [])
    if cts:
        bits.append("国家 " + " ".join(cts[:10]) + ("…" if len(cts) > 10 else ""))
    if key == FALLBACK_REGION:
        bits.append("判不出地区时的兜底")
    elif not bits:
        bits.append("未配置归属")
    return " · ".join(bits)


def _category_meta(cfg):
    """特殊 6 键 + 每个地区档的 Movie/Show → {分类键: (中文名, 分组, 说明)}。"""
    meta = dict(_SPECIAL_META)
    norm = normalize_regions((cfg or {}).get("regions"))
    for key in norm["order"]:
        it = norm["items"][key]
        label = it.get("label") or key
        desc = _region_desc(key, it)
        meta[key + "Movie"] = (label + "电影", "region", desc)
        meta[key + "Show"] = (label + "剧集", "region", desc)
    return meta


def _regions_out(cfg):
    """给前端表格用的地区档(规范化后的 {order, items}, 缺项已补默认)。"""
    norm = normalize_regions((cfg or {}).get("regions"))
    return {"order": norm["order"], "items": norm["items"]}


class CategoriesBody(BaseModel):
    categories: dict = {}     # {分类键: 目录名} —— 只接受白名单键
    cloud_root: str = ""      # 媒体库根(如 /Cloud); 留空 = 不改
    regions: Optional[dict] = None   # 地区档配置; None = 不改(省略该字段的旧前端也能存)


# ---- 请求体模型 ----
class PushItem(BaseModel):
    magnet: str
    title: str = ""
    content_type: str = "movie"   # movie | tv
    language: str = ""
    countries: list = []
    adult: bool = False
    toFolder: str = ""


class ApplyBody(BaseModel):
    names: list = []      # 要执行的条目名(留空且 all=false 则什么都不做)
    all: bool = False     # true = 执行全部可整理的条目
    # 单次最多执行几个; 0 = 用服务端默认 100(不能默认 20: Pydantic 会把"没传"填成 20,
    # 前端不带 limit 的「执行全部」就被静默截断 —— 2026-09-29 修)
    limit: int = 0


class MatchBody(BaseModel):
    name: str             # 离线条目名(手动匹配的 key)
    query: str = ""       # 候选搜索词(留空 = 用条目名/原目录名)
    tmdb_id: int = 0      # 选定的 TMDB id(0 = 取消手动指定, 回到自动反查)
    kind: str = ""        # movie | tv
    title: str = ""
    year: str = ""


# ---- 整理任务(后台) ----
_JOBS = {}   # job_id -> {status: running|done|error, count, results, error, started_at}
# 整理执行互斥锁: 同一进程内只允许一个 /organize/apply 后台任务在跑。两个 worker 同时
# 搬/改名/删广告同一批文件会在 CD2 上交叉操作 → 数据错乱(2026-10-03 子代理发现, 加守卫)。
_ORGANIZE_LOCK = threading.Lock()


def _new_job():
    jid = uuid.uuid4().hex[:12]
    _JOBS[jid] = {
        "status": "running", "count": 0, "results": [], "error": "", "note": "",
        "started_at": time.time(),
    }
    # 防膨胀: 只保留最近 50 个(进程内, 重启即清)
    if len(_JOBS) > 50:
        for k in sorted(_JOBS, key=lambda x: _JOBS[x]["started_at"])[:len(_JOBS) - 50]:
            _JOBS.pop(k, None)
    return jid


# ---------------------------------------------------------------------------
# 推送离线下载
# ---------------------------------------------------------------------------
@router.post("/push")
async def api_push(item: PushItem, cfg: dict = Depends(get_config)):
    try:
        from clients.clouddrive import client as cd2
        # ⚠️ parse_info_hash 也要在这儿绑上 —— 原来只 import 了 build_task, 下面
        # push.parse_info_hash 必然 NameError 被 except 吞掉, 推送标记**从来没写进去过**。
        from scripts.push import build_task, parse_info_hash
        from lib import state
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"模块加载失败: {e}")

    to_folder = item.toFolder or cfg.get("clouddrive2", {}).get("offline_root", "/Offline")
    try:
        res = await run_in_threadpool(
            cd2.add_offline, cfg, item.magnet, to_folder, skill_root()
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"推送到 CD2 失败: {e}")

    # 写入队列, 与 CLI / check.py 共用状态
    meta = {
        "title": item.title,
        "content_type": item.content_type,
        "language": item.language,
        "countries": item.countries or [],
        "adult": item.adult,
    }
    # 写入队列, 与 CLI / check.py 共用状态(add_task 返回 False=已存在同 hash/magnet 的任务, 不重复入队)
    task = build_task(item.magnet, meta, to_folder)
    try:
        queued = bool(state.add_task(task))
    except Exception as e:  # noqa: BLE001
        # CD2 已收到下载, 只是本地队列没记上 —— 如实返回, 不再谎报 queued
        print(f"[push] 写入本地队列失败: {e}", file=sys.stderr)
        queued = False
    # 写推送标记(落库, 重启不丢; info_hash 解析失败则跳过标记)
    try:
        from db.database import SessionLocal
        from db import repositories as repo
        s = SessionLocal()
        try:
            repo.mark_pushed(s, parse_info_hash(item.magnet), item.magnet, item.title, target="cd2")
            s.commit()
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        print(f"[push] 写入推送标记失败: {e}", file=sys.stderr)
    return {"ok": True, "cd2": res, "queued": queued}


# ---------------------------------------------------------------------------
# 文件整理: 清广告 → 改名 title.year.[tt|tmdb]id → 归位媒体库
# ---------------------------------------------------------------------------
@router.get("/organize/files")
async def api_organize_files(limit: int = 50, only: str = "", cfg: dict = Depends(get_config)):
    """扫描离线目录,返回「整理计划」(每个条目: 要删的广告、新目录名、目标库)。

    实现要点(踩坑记录):
      - 【不要】用 ListOfflineFilesByPath 拿"已完成任务列表": 它是 unary 且一次性返回该目录
        下全部离线任务,任务量大时要几十秒(实测 4769 条任务约 49s),网页请求必然超时。
        离线根目录下【能看到的条目本身就是已下载完成的产物】,直接 GetSubFiles 即可,毫秒级。
      - GetSubFiles 的字段是 fullPathName / isDirectory,不是 path / isFolder(旧代码取错字段,
        导致 path 恒为 None、移动操作必然失败)。
      - 判定目录用 isDirectory(bool);protojson 输出的是枚举名,不能比 fileType 的数值。
      - 每个条目要反查一次元数据(拿 IMDB/TMDB 号; 本地 TMDB 缓存 → TMDB 直连),故用 limit 控住单次条目数。
    """
    try:
        from clients.clouddrive import client as cd2
        from scripts import organize
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"模块加载失败: {e}")

    org = cfg.get("organize") or {}
    if org.get("enabled") is False:
        return {"offline_root": organize.offline_root(cfg), "enabled": False,
                "plans": [], "stats": {}, "msg": "整理功能已在 config.organize.enabled=false 关闭"}

    try:
        plans = await run_in_threadpool(
            organize.build_plans, cfg, skill_root(),
            (only or None), max(1, min(int(limit or 50), 200)),
            # ⚠️ 预览必须只读: 不写 title/title_checked、不消耗豆瓣"每条一次"的核对标记
            #    (与 CLI dry-run 同口径, organize.run 的 read_only=not apply)。漏传的话
            #    打开/刷新整理页就等于执行了一轮写库 —— 2026-09-29 修。
            read_only=True,
        )
    except Exception as e:  # noqa: BLE001
        # 元数据主源是 TMDB, 文案里就以 TMDB 为准
        raise HTTPException(502, f"读取 CD2 / TMDB 失败: {e}")

    # 精简字段给前端(体积控制)
    out = []
    for p in plans:
        m = p.get("meta") or {}
        out.append({
            "name": p["name"],
            "path": p["source"],
            "isFolder": p["is_dir"],
            "status": p["status"],
            "reason": p["reason"],
            "media_count": p["media_count"],
            "media_bytes": p["media_bytes"],
            "ad_count": p["ad_count"],
            "ad_files": p["ad_files"][:20],
            "junk_files": p["junk_files"][:20],
            "asset_files": (p.get("asset_files") or [])[:20],
            "nfo_files": (p.get("nfos") or [])[:20],
            "title": m.get("title"),
            "year": m.get("year"),
            "imdb_id": m.get("imdb_id") or "",
            "tmdb_id": m.get("tmdb_id"),
            "kind": m.get("kind"),
            "matched_query": m.get("matched_query"),
            "match_score": m.get("match_score"),
            "new_name": p["new_name"],
            "target_root": p["target_root"],
            "target": p["target"],
            # 只读预览: 每个媒体文件的预期改名/落点(不触发 CD2 写操作)
            "preview": organize.preview_plan_files(cfg, p, skill_root()),
        })

    # 附加信息: 离线配额 + 最近任务进度(都很快,失败不影响主流程)
    stats = {}
    try:
        cd2cfg = cfg.get("clouddrive2", {}) or {}
        stats["quota"] = await run_in_threadpool(cd2.get_offline_quota, cfg)
        recent = await run_in_threadpool(
            cd2.list_offline_all, cfg, min(int(cd2cfg.get("offline_max_pages") or 2), 3))
        stats["recent_total"] = len(recent)
        running = [t for t in recent if not cd2.is_finished(t.get("status"))]
        stats["running"] = [
            {"name": t.get("name"), "status": cd2.status_text(t.get("status")),
             "status_raw": t.get("status"), "progress": t.get("percendDone", 0),
             "infoHash": t.get("infoHash")}
            for t in running[:20]
        ]
    except Exception:  # noqa: BLE001
        pass

    return {
        "offline_root": organize.offline_root(cfg),
        "movie_root": organize.root_for_kind(cfg, "movie"),
        "tv_root": organize.root_for_kind(cfg, "tv"),
        "categories": list((cfg.get("categories") or {}).keys()),
        "plans": out,
        "stats": stats,
    }


@router.get("/organize/match")
async def api_organize_match_candidates(name: str, query: str = "", cfg: dict = Depends(get_config)):
    """手动重匹配 —— 第 1 步: 给某离线条目拉 TMDB 候选条目。

    自动反查会错配(如 基督山伯爵.2024 被反查到 1961 版同名条目), 让用户手动选对。
    query 缺省用条目名(原目录名/散落文件名); 返回 movie+tv 混合卡片(与浏览/搜索同形)。
    """
    if not (name or query).strip():
        raise HTTPException(400, "name 或 query 不能为空")
    from clients.tmdb import client as tmdb_client
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(503, "未配置 tmdb.api_key, 无法搜索候选")
    q = (query or name).strip()
    try:
        cards = await tmdb_client.search_cards(cfg, q)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"TMDB 搜索失败: {e}")
    return {"query": q, "results": cards}


@router.post("/organize/match")
async def api_organize_set_match(body: MatchBody, cfg: dict = Depends(get_config)):
    """手动重匹配 —— 第 2 步: 把选定条目(或取消)持久化到 DB。

    tmdb_id>0: 拉该条目详情(补 imdb/年份/国家/语言, 供分类与 NFO), 存为手动匹配。
    tmdb_id=0: 删除覆盖, 回到自动反查。返回更新后的匹配摘要。
    """
    if not body.name:
        raise HTTPException(400, "name 不能为空")
    from scripts import organize
    if body.tmdb_id <= 0:
        def _clear():
            organize.set_manual_match(body.name, None)
            return None
        await run_in_threadpool(_clear)
        return {"ok": True, "manual": None, "msg": f"已取消「{body.name}」的手动指定, 恢复自动反查"}

    from clients.tmdb import client as tmdb_client
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(503, "未配置 tmdb.api_key, 无法加载该条目")

    def _save():
        kind = body.kind if body.kind in ("movie", "tv") else "movie"
        d = tmdb_client.detail_sync(cfg, kind, body.tmdb_id)
        if not d:
            raise RuntimeError(f"TMDB 无此条目(id={body.tmdb_id})")
        meta = {
            "tmdb_id": d.get("tmdb_id") or body.tmdb_id,
            "imdb_id": d.get("imdb_id") or "",
            "tvdb_id": d.get("tvdb_id"),
            "title": d.get("title") or body.title or "",
            "originalTitle": d.get("originalTitle") or "",
            "year": d.get("year") or body.year or "",
            "kind": d.get("kind") or kind,
            "countries": list(d.get("countries") or []),
            # ⚠️ 分类必需字段必须一并带上(与自动反查两条路径同口径):
            # 缺 genres → 手动指定的动漫(16)/纪录(99)落地区目录; 缺 original_language
            # → 地区判定退化。certification/adult 2026-09-22 起已不参与归类(成人内容
            # 改由 Jellyfin 按分级控制), 仍照存一份供展示/兼容。
            "genres": list(d.get("genre_ids") or []),
            "certification": d.get("certification") or "",
            "original_language": d.get("original_language") or "",
            "adult": bool(d.get("adult")),
        }
        organize.set_manual_match(body.name, meta)
        return meta
    try:
        meta = await run_in_threadpool(_save)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"保存手动匹配失败: {e}")
    return {"ok": True, "manual": meta, "msg": f"「{body.name}」已指定为 {meta.get('title')} ({meta.get('year')})"}


@router.post("/organize/apply")
async def api_organize_apply(body: ApplyBody, cfg: dict = Depends(get_config)):
    """后台启动整理: 搬前探测 → 删广告/清推广 → 改名/工作区整理 → 归位 → 写 NFO。

    body.names 指定要处理的条目名; body.all=true 则处理全部
    (ok/merge 归位, duplicate/upgrade 只清广告), 受 body.limit(0=默认 100, 上限 200)
    限制, 被截断时会在 job.note 里出声。
    立即返回 job_id, 前端轮询 GET /organize/apply/{job_id} 取进度与结果。
    """
    # 默认上限 100(原来是 20): 前端「执行全部/执行选中」不传 limit, 20 会把用户
    # 在确认框里数过的 N 条**静默截断**成 20 条; 传了就按传的来, 上限 200(2026-09-29 修)。
    limit = max(1, min(int(body.limit or 100), 200))
    # 并发守卫(在建 job 之前抢锁): 抢不到 = 已有整理任务在跑 → 409 提示, 绝不并发起
    # 第二个 worker(双 worker 会同时搬/改同一批文件)。建 job 前抢, 避免留一个空 job。
    if not _ORGANIZE_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有整理任务正在运行, 请等待其完成后再执行")
    base = skill_root()
    jid = _new_job()
    job = _JOBS[jid]

    def _worker():
        # 落库日志(重启不丢, 失败明细可在「整理记录」里查) —— 进程内 _JOBS 仅作实时进度
        # ⚠️ log_organize_start 只 flush 不 commit; session.close() 会回滚未提交事务,
        #    导致起始行从未落库、后续按 id 查不到 → 日志恒为空。必须显式 commit 并
        #    把主键捕获成 int(id 在会话关闭后仍可安全引用)。
        log_id = None
        scope = "all" if body.all else "、".join((body.names or [])[:5])
        try:
            from db.database import SessionLocal
            from db import repositories as repo
            s = SessionLocal()
            try:
                row = repo.log_organize_start(s, "web", scope)
                s.commit()
                log_id = row.id
            finally:
                s.close()
        except Exception:  # noqa: BLE001
            log_id = None

        def _db_call(fn, *args):
            if log_id is None:
                return
            try:
                from db.database import SessionLocal
                from db.models import OrganizeLog
                s = SessionLocal()
                try:
                    row = s.get(OrganizeLog, log_id)
                    if row:
                        fn(s, row, *args)
                        s.commit()
                finally:
                    s.close()
            except Exception:  # noqa: BLE001
                pass

        def _db_result(result: dict, error: str = ""):
            def _apply(s, row, r, err):
                r = dict(r)
                if err:
                    r["error"] = err
                repo.log_organize_result(s, row, r)
            _db_call(_apply, result, error)

        def _finish_db_log(status, error=""):
            _db_call(lambda s, row, st, er: repo.log_organize_end(s, row, st, er,
                                                                 total=job.get("count") or 0),
                     status, error)

        try:
            from scripts import organize
            plans = organize.build_plans(cfg, base)
            wanted = set(body.names or [])
            chosen = []
            for p in plans:
                # duplicate = 库里已有同名条目: 不重复归位,但仍清掉源目录里的广告
                # merge = 剧库已有该剧但缺本条的季: 补季合并(库内已有内容不受影响)
                # upgrade = 库里已有但这条规格更高(升级版): 与 duplicate 同口径,
                #           只清广告 —— /Cloud 禁删, 不会自动换上(2026-09-29 补,
                #           原来它被整个丢掉, 连广告都不清、汇总里也见不到)
                if p["status"] not in ("ok", "duplicate", "merge", "upgrade"):
                    continue
                if body.all or p["name"] in wanted:
                    chosen.append(p)
            if len(chosen) > limit:
                # 截断必须出声: 否则"执行全部 57 个"只跑了 20 个, 界面只显示 20 条结果
                job["note"] = (f"选中 {len(chosen)} 条, 超出单次上限 {limit} 条, "
                               f"本次只处理前 {limit} 条(其余再点一次即可)")
            chosen = chosen[:limit]
            job["count"] = len(chosen)

            for p in chosen:
                try:
                    r = organize.apply_plan(cfg, p, base_dir=base, log=lambda *a, **k: None)
                    item = {
                        "name": p["name"],
                        "status": p["status"],
                        "ok": True,
                        "new_name": p["new_name"],
                        "target": p["target"],
                        "deleted": len(r.get("deleted") or []),
                        "cleaned_names": len(r.get("cleaned_names") or []),
                        "nfo": r.get("nfo"),
                        "media_renamed": len(r.get("renamed_media") or {}),
                        "moved": r.get("moved"),
                        "renamed": r.get("renamed"),
                        "skipped": r.get("skipped"),
                        # ⚠️ 成功但被闸门拒绝/部分跳过的原因也要带上, 否则界面看着"没反应"
                        "note": (r.get("skipped") or "") + (
                            "" if r.get("nfo") else ";NFO 未写入(库内已有或校验未过)"),
                    }
                    job["results"].append(item)
                    _db_result(item)
                except Exception as e:  # noqa: BLE001
                    item = {"name": p["name"], "status": p["status"], "ok": False,
                            "error": str(e)[:300]}
                    job["results"].append(item)
                    _db_result(item, error=str(e)[:300])
                    # 失败详情进 stderr(服务日志可见), 不再只留在前端 console
                    print(f"[organize] 条目失败: {p['name']}: {str(e)[:300]}", file=sys.stderr)
            job["status"] = "done"
            _finish_db_log("success")
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(e)[:500]
            print(f"[organize] 任务失败: {str(e)[:500]}", file=sys.stderr)
            _finish_db_log("error", str(e)[:500])

    def _run_guarded():
        try:
            _worker()
        finally:
            _ORGANIZE_LOCK.release()   # 无论 worker 成功/异常都释放, 让下一次整理能进

    try:
        threading.Thread(target=_run_guarded, daemon=True).start()
    except Exception:  # noqa: BLE001  起线程失败(极罕见)也要释放, 否则永久锁死
        _ORGANIZE_LOCK.release()
        raise
    return {"ok": True, "job_id": jid,
            "msg": "整理已在后台启动, 轮询 GET /api/organize/apply/" + jid + " 查看进度"}


@router.get("/organize/apply/{job_id}")
async def api_organize_apply_status(job_id: str):
    """轮询整理任务: running 时 results 是已完成的部分; done 后 results 是全部结果。"""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期(服务重启后清空)")
    return {
        "job_id": job_id,
        "status": job["status"],          # running | done | error
        "count": job["count"],            # 选中的条目总数
        "done": len(job["results"]),      # 已完成的条目数(进度)
        "error": job["error"],
        "note": job.get("note") or "",    # 截断提示等(前端 toast)
        "results": job["results"],
    }


@router.get("/organize/logs")
async def api_organize_logs(limit: int = 20):
    """整理执行的历史记录(落库, 服务重启不丢)。

    每条含 status/ok/failed 汇总 + results 明细(失败项带 error, 成功但被闸门
    拒绝的带 note)。前端「整理记录」面板轮询此接口。
    """
    limit = max(1, min(int(limit or 20), 100))
    s = SessionLocal()
    try:
        return {"logs": repo.list_organize_logs(s, limit)}
    finally:
        s.close()


@router.post("/organize/finish")
async def api_organize_finish(body: dict = Body(default={}), cfg: dict = Depends(get_config)):
    mode = body.get("mode", "all")
    if mode not in ("movie", "tv", "all"):
        raise HTTPException(400, "mode 仅支持 movie / tv / all")

    def _worker():
        try:
            from scripts import finish as jf_finish
            jf_finish.refresh_jellyfin(cfg)
        except Exception:
            pass

    # 后台执行(刮削可能很久), 立即返回, 不阻塞网页
    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "mode": mode, "started": True,
            "msg": "已在后台刷新 Jellyfin, 稍后到 Jellyfin 查看结果"}


# ---------------------------------------------------------------------------
# 分类规则管理(管理页「分类规则」子页签): 分类键→目录名 映射 + 媒体库根
# ---------------------------------------------------------------------------
def _default_categories(cfg):
    """config.categories 缺失时用分类键自身兜底(与 classify._emit 的回退一致)。"""
    return {k: (cfg.get("categories") or {}).get(k, k) for k in _category_meta(cfg)}


@router.get("/organize/categories")
async def api_organize_categories_get(cfg: dict = Depends(get_config)):
    """分类规则: 全部分类键(特殊 6 + 地区档 × 2)的目录名映射 + 地区档配置 + 库内实际目录。

    键数随地区档变(默认 6 特殊 + 6 地区档 × 2 = 18);地区档页签里可增删改。
    exists 来自一次 CD2 GetSubFiles(cloud_root)(毫秒级); CD2 不可达时 exists 全为 null, 不报错。
    """
    categories = _default_categories(cfg)
    org = cfg.get("organize") or {}
    cloud_root = (org.get("cloud_root") or "/Cloud").rstrip("/")

    def _q():
        existing = {}
        try:
            from clients.clouddrive import client as cd2
            files = cd2.get_subfiles(cfg, cloud_root, timeout=15)
            existing = {f.get("name") for f in files if cd2.is_dir(f)}
        except Exception:  # noqa: BLE001  CD2 不可达 → exists 全 null, 不影响规则读写
            existing = None
        return existing

    existing = await run_in_threadpool(_q)
    rows = []
    for key, (label, group, desc) in _category_meta(cfg).items():
        folder = categories.get(key, key)
        rows.append({
            "key": key, "label": label, "group": group, "desc": desc,
            "folder": folder,
            "path": cloud_root + "/" + folder,
            "exists": (existing is not None) and (folder in existing),
        })
    return {"cloud_root": cloud_root, "existing_checkable": existing is not None,
            "rows": rows, "regions": _regions_out(cfg)}


@router.put("/organize/categories")
async def api_organize_categories_put(body: CategoriesBody, cfg: dict = Depends(get_config)):
    """保存分类规则 → 写回 app_config 表(updated_at 热加载, 下一次整理立即生效, 无需重启)。

    校验(防把整理归位写坏):
      - regions 带了就先过 lib.classify.validate_regions(结构/键名/撞车/Ot 必留/跨档重复);
      - 分类键必须落在「特殊 6 + 当前地区档生成的键」白名单内(地区档可增删, 白名单跟着变);
      - 目录名非空 / 无路径分隔符与控制字符 / ≤64 字符;
      - cloud_root 留空不改; 要改必须以 / 开头且不含 //。
    """
    # 1) 地区档(可选): 先校验 → 规范化 → 与本次提交的目录名一起算最终白名单
    regions = None
    if body.regions is not None:
        try:
            validate_regions(body.regions)
        except ValueError as e:
            raise HTTPException(400, f"地区档配置不合法: {e}")
        norm = normalize_regions(body.regions)
        regions = {"order": norm["order"], "items": norm["items"]}
    final_cfg = dict(cfg)
    if regions is not None:
        final_cfg["regions"] = regions
    meta = _category_meta(final_cfg)

    cats = dict(cfg.get("categories") or {})
    changed = 0
    for key, folder in (body.categories or {}).items():
        if key not in meta:
            raise HTTPException(
                400, f"未知分类键: {key}(特殊类型键固定, 地区键只能通过「地区档」增减, 不能自造)")
        folder = (folder or "").strip()
        if not folder:
            raise HTTPException(400, f"分类 {key} 的目录名不能为空")
        if len(folder) > 64:
            raise HTTPException(400, f"分类 {key} 的目录名过长(>64)")
        if _DIR_NAME_BAD.search(folder):
            raise HTTPException(400, f"分类 {key} 的目录名含非法字符: {folder}")
        if folder in (".", ".."):
            raise HTTPException(400, f"分类 {key} 的目录名不能是 {folder}")
        if cats.get(key) != folder:
            cats[key] = folder
            changed += 1

    # 2) 地区档变了 → 清掉已不存在的地区键的目录名(键没了, 名字留着也只是死配置)
    if regions is not None:
        cats = {k: v for k, v in cats.items() if k in meta}

    cloud_root = (cfg.get("organize") or {}).get("cloud_root") or "/Cloud"
    new_root = (body.cloud_root or "").strip()
    if new_root and new_root != cloud_root:
        if not new_root.startswith("/") or "//" in new_root or new_root in (".", ".."):
            raise HTTPException(400, f"媒体库根路径不合法: {new_root}(须以 / 开头, 如 /Cloud)")
        cloud_root = new_root.rstrip("/")

    # 写回配置(app_config 表; updated_at 变化 → 下一次请求热加载, 下次整理生效)
    def _write():
        data = dict(get_config())
        data["categories"] = cats
        if regions is not None:
            data["regions"] = regions
        data.setdefault("organize", {})["cloud_root"] = cloud_root
        save_config(data)
    try:
        await run_in_threadpool(_write)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"写入配置失败: {e}")

    region_changed = regions is not None and (cfg.get("regions") or {}) != regions
    n_region_keys = len([k for k in meta if k not in _SPECIAL_META])
    parts = []
    if changed:
        parts.append(f"{changed} 个目录变更")
    if region_changed:
        parts.append(f"地区档已更新(当前 {len(regions['order'])} 档 → {n_region_keys} 个地区分类键)")
    msg = ("已保存 " + "; ".join(parts)) if parts else "没有变更(目录名与原值相同)"
    return {"ok": True, "changed": changed, "cloud_root": cloud_root,
            "region_changed": region_changed, "regions": _regions_out(final_cfg),
            "msg": msg}
