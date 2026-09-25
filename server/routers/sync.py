"""同步与本地库路由

触发同步(后台线程):
  POST /api/sync/tmdb?scope=all&full=
  POST /api/sync/jellyfin?scope=all|libraries|items|episodes
  POST /api/sync/jf-scanner?mode=full|recent
  POST /api/sync/availability

读取本地 SQLite 已同步数据:
  GET /api/sync/logs?source=
  GET /api/local/libraries
  GET /api/local/items?library_id=&q=&limit=
"""
import asyncio
import inspect
import threading

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from server.auth import require_auth
from server.config import get_config
from db.database import SessionLocal, init_db
from db import repositories as repo
from db.models import SyncLog

router = APIRouter(prefix="/api", tags=["sync"], dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _serialize(rows):
    return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]


def _create_log(source: str, scope: str) -> int:
    session = SessionLocal()
    try:
        log = repo.log_sync_start(session, source, scope)
        session.commit()
        return log.id
    finally:
        session.close()


def _bg_sync(log_id: int, source: str, scope: str, coro_func):
    """在后台线程里跑同步函数,并写回同步日志。

    ⚠️ 各脚本的 `run()` **同步性并不一致**:
      · `sync_jellyfin.run` / `sync_tmdb.run` 是 **async** → 调用返回 coroutine;
      · `sync_jf_scanner.run` / `availability_sync.run` 是 **同步** → 调用直接返回结果
        (它们内部自己 `asyncio.run(_run_async(...))`)。
    旧写法无条件 `asyncio.run(coro_func(scope))`, 对同步的那两个会抛
    `An asyncio.Future, a coroutine or an awaitable is required` —— 于是
    `/api/sync/jf-scanner` 与 `/api/sync/availability` 两个 HTTP 入口**一直是坏的**
    (CLI 不受影响, 所以一直没被发现)。这里按返回值是否 awaitable 分派。
    """
    session = SessionLocal()
    try:
        log = session.get(SyncLog, log_id)
        res = coro_func(scope)
        total = asyncio.run(res) if inspect.isawaitable(res) else res
        repo.log_sync_end(session, log, "success", total)
        session.commit()
    except Exception as e:  # noqa: BLE001
        try:
            log = session.get(SyncLog, log_id)
            repo.log_sync_end(session, log, "error", 0, str(e)[:2000])
            session.commit()
        except Exception:
            pass
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 触发同步
# ---------------------------------------------------------------------------
@router.post("/sync/tmdb")
async def sync_tmdb(scope: str = Query("all"), full: bool = Query(False),
                    limit: int = Query(0), cfg: dict = Depends(get_config)):
    """同步 TMDB 元数据到本地缓存(种子=Jellyfin 库)。full=全量重刷。"""
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise HTTPException(400, "未配置 tmdb.api_key(免费注册: themoviedb.org → Settings → API)")
    init_db()
    log_id = await run_in_threadpool(_create_log, "tmdb", f"all{'(全量)' if full else '(增量)'}")

    def _run(_scope_arg):
        from scripts import sync_tmdb as s
        return s.run(_scope_arg, full=full, limit=limit)

    threading.Thread(target=_bg_sync, args=(log_id, "tmdb", f"all{'(全量)' if full else '(增量)'}", _run),
                     daemon=True).start()
    return {"ok": True, "log_id": log_id,
            "msg": f"已在后台启动 TMDB 同步({'全量重刷' if full else '增量'})"}


@router.post("/sync/jellyfin")
async def sync_jellyfin(scope: str = Query("all"), full: bool = Query(False),
                        cfg: dict = Depends(get_config)):
    if scope not in ("all", "libraries", "items", "episodes"):
        raise HTTPException(400, "scope 仅支持 all / libraries / items / episodes")
    init_db()
    log_id = await run_in_threadpool(_create_log, "jellyfin", f"{scope}{'(全量)' if full else '(增量)'}")

    def _run(_scope_arg):
        # _bg_sync 会把(展示用)scope 字符串传进来, 这里用闭包里的真值
        from scripts import sync_jellyfin as s
        return s.run(scope, full=full)

    threading.Thread(target=_bg_sync, args=(log_id, "jellyfin", f"{scope}{'(全量)' if full else '(增量)'}", _run), daemon=True).start()
    return {"ok": True, "log_id": log_id,
            "msg": f"已在后台启动 Jellyfin 同步({'全量重建' if full else '增量'})"}


# ---------------------------------------------------------------------------
# 读取本地库
# ---------------------------------------------------------------------------
@router.post("/sync/jf-scanner")
async def sync_jf_scanner(mode: str = Query("full"), limit: int = Query(0),
                          cfg: dict = Depends(get_config)):
    """Jellyfin 可用性扫描(写 media/season)。mode=full|recent。"""
    if mode not in ("full", "recent"):
        raise HTTPException(400, "mode 仅支持 full / recent")
    init_db()
    log_id = await run_in_threadpool(_create_log, "jellyfin", f"jf-scan:{mode}")

    def _run(_scope_arg):
        from scripts import sync_jf_scanner as s
        stats = s.run(mode=mode, limit=limit)
        # 返回"处理条数"整数供日志 items_synced 用
        return int(stats.get("movie", 0) + stats.get("show", 0))

    threading.Thread(target=_bg_sync, args=(log_id, "jellyfin", f"jf-scan:{mode}", _run),
                     daemon=True).start()
    return {"ok": True, "log_id": log_id,
            "msg": f"已在后台启动 Jellyfin 可用性扫描({mode})"}


@router.post("/sync/availability")
async def sync_availability(cfg: dict = Depends(get_config)):
    """可用性对账(标记已从 Jellyfin 删除的作品/季为 DELETED)。"""
    init_db()
    log_id = await run_in_threadpool(_create_log, "jellyfin", "availability")

    def _run(_scope_arg):
        from scripts import availability_sync as s
        stats = s.run(dry=False)
        return int(stats.get("movie_deleted", 0) + stats.get("show_deleted", 0)
                   + stats.get("seasons_deleted", 0))

    threading.Thread(target=_bg_sync, args=(log_id, "jellyfin", "availability", _run),
                     daemon=True).start()
    return {"ok": True, "log_id": log_id, "msg": "已在后台启动可用性对账"}


@router.get("/sync/logs")
async def sync_logs(source: str = Query(""), limit: int = 50):
    def _q():
        s = SessionLocal()
        try:
            return _serialize(repo.get_sync_logs(s, source, limit))
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.get("/local/libraries")
async def local_libraries():
    def _q():
        s = SessionLocal()
        try:
            return _serialize(repo.get_libraries(s))
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.get("/local/items")
async def local_items(library_id: str = Query(""), q: str = Query(""), limit: int = 500):
    def _q():
        s = SessionLocal()
        try:
            return _serialize(repo.get_items(s, library_id, q, limit))
        finally:
            s.close()
    return await run_in_threadpool(_q)
