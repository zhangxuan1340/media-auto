"""MediaAuto Web —— 追踪(演员新作 / 剧集新季)API

- GET  /api/track            追踪列表(含各条状态/上次结果)
- POST /api/track            添加追踪 ?kind=person|show&ref_id=&name=  (自动建基线)
- DELETE /api/track          移除 ?kind=&ref_id=
- POST /api/track/check      手动触发一次检查(后台跑, 返回 job id)
- GET  /api/track/check/status?job=   轮询检查进度
- GET  /api/track/settings   全局设置(自动推送开关 + 4K/1080p 大小范围)
- POST /api/track/settings   更新设置
- POST /api/track/auto_push  单条覆盖 ?kind=&ref_id=&auto_push=on|off|follow
"""
import json
import threading
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from server.auth import require_auth
from server.config import get_config
from db.database import SessionLocal
from db.models import Track
from db import repositories as repo
from scripts import track_check as tc  # settings 键与读取逻辑单一来源

router = APIRouter(prefix="/api/track", tags=["track"],
                   dependencies=[Depends(require_auth)])

# 手动检查的后台任务(进程内): job_id → {status, result, started_at, error}
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def _get_track(session, kind: str, ref_id: int) -> Track:
    t = session.query(Track).filter_by(kind=kind, ref_id=ref_id).one_or_none()
    if t is None:
        raise HTTPException(404, f"未追踪: {kind}/{ref_id}")
    return t


def _row(t: Track) -> dict:
    return {
        "id": t.id,
        "kind": t.kind,
        "refId": t.ref_id,
        "name": t.name,
        "autoPush": t.auto_push,          # None=跟随全局
        "lastCheckedAt": t.last_checked_at.isoformat() if t.last_checked_at else None,
        "lastResult": t.last_result or "",
        "createdAt": t.created_at.isoformat() if t.created_at else None,
    }


# ---------------------------------------------------------------------------
# 列表 / 增删
# ---------------------------------------------------------------------------
@router.get("")
async def list_tracks(cfg: dict = Depends(get_config)):
    def _q():
        s = SessionLocal()
        try:
            return [_row(t) for t in s.query(Track).order_by(Track.created_at).all()]
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.post("")
async def add_track(kind: str = Query(...), ref_id: int = Query(..., alias="ref_id"),
                    name: str = Query(""), cfg: dict = Depends(get_config)):
    """添加追踪。kind=person 用 TMDB person id; kind=show 用 TMDB tv id。

    name 缺省时按需从 TMDB 取(演员名/剧名), 保证列表显示可读。
    基线在**首次 check** 时初始化(见 track_check), 这里只登记。
    """
    if kind not in ("person", "show"):
        raise HTTPException(400, "kind 必须是 person 或 show")
    if ref_id <= 0:
        raise HTTPException(400, "ref_id 非法")

    async def _q():
        resolved = name
        if not resolved:
            # 取可读名(失败不致命, 列表用 ref_id 兜底)
            from clients.tmdb import client as tmdb
            try:
                if kind == "person":
                    d = await tmdb.person_details(cfg, ref_id)
                    resolved = (d or {}).get("name", "") or ""
                else:
                    d = await tmdb.detail(cfg, "tv", ref_id)
                    resolved = (d or {}).get("title", "") or ""
            except Exception:  # noqa: BLE001
                resolved = ""
        s = SessionLocal()
        try:
            exist = s.query(Track).filter_by(kind=kind, ref_id=ref_id).one_or_none()
            if exist:
                return _row(exist)
            t = Track(kind=kind, ref_id=ref_id, name=resolved or f"{kind}/{ref_id}")
            s.add(t)
            s.commit()
            s.refresh(t)
            return _row(t)
        finally:
            s.close()
    return await _q()


@router.get("/has")
async def track_has(kind: str = Query(...), ref_id: int = Query(..., alias="ref_id")):
    """是否已追踪(详情页按钮状态用, 轻量)。"""
    def _q():
        s = SessionLocal()
        try:
            t = s.query(Track).filter_by(kind=kind, ref_id=ref_id).one_or_none()
            return {"tracked": t is not None,
                    "autoPush": t.auto_push if t else None,
                    "name": t.name if t else ""}
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.delete("")
async def remove_track(kind: str = Query(...), ref_id: int = Query(..., alias="ref_id")):
    def _q():
        s = SessionLocal()
        try:
            t = s.query(Track).filter_by(kind=kind, ref_id=ref_id).one_or_none()
            if t is None:
                return {"ok": False, "removed": False}
            s.delete(t)
            s.commit()
            return {"ok": True, "removed": True}
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.post("/auto_push")
async def set_auto_push(kind: str = Query(...), ref_id: int = Query(..., alias="ref_id"),
                        mode: str = Query("follow")):
    """单条自动推送覆盖: on / off / follow(跟随全局)。"""
    if mode not in ("on", "off", "follow"):
        raise HTTPException(400, "mode 必须是 on/off/follow")
    val = True if mode == "on" else (False if mode == "off" else None)

    def _q():
        s = SessionLocal()
        try:
            t = _get_track(s, kind, ref_id)
            t.auto_push = val
            s.commit()
            s.refresh(t)
            return _row(t)
        finally:
            s.close()
    return await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# 全局设置(自动推送开关 + 大小范围)
# ---------------------------------------------------------------------------
def _settings_dict(s):
    st = tc.get_track_settings(s)
    return {
        "auto_push": st["auto_push"],
        "size_4k_min_gb": st["size_4k"][0],
        "size_4k_max_gb": st["size_4k"][1],
        "size_1080_min_gb": st["size_1080"][0],
        "size_1080_max_gb": st["size_1080"][1],
    }


@router.get("/settings")
async def get_settings():
    def _q():
        s = SessionLocal()
        try:
            return _settings_dict(s)
        finally:
            s.close()
    return await run_in_threadpool(_q)


@router.post("/settings")
async def set_settings(auto_push: bool = Query(None),
                       size_4k_min_gb: float = Query(None),
                       size_4k_max_gb: float = Query(None),
                       size_1080_min_gb: float = Query(None),
                       size_1080_max_gb: float = Query(None),
                       clear: str = Query("")):
    """大小范围: 传了才改; 留空想清回"不限"就放进 clear(逗号分隔键名)。"""
    clear_keys = {k.strip() for k in clear.split(",") if k.strip()}
    keymap = {"size_4k_min": tc.KEY_4K_MIN, "size_4k_max": tc.KEY_4K_MAX,
              "size_1080_min": tc.KEY_1080_MIN, "size_1080_max": tc.KEY_1080_MAX}

    def _q():
        s = SessionLocal()
        try:
            if auto_push is not None:
                repo.set_setting(s, tc.KEY_AUTO_PUSH, "true" if auto_push else "false")
            for key, val in (
                (tc.KEY_4K_MIN, size_4k_min_gb), (tc.KEY_4K_MAX, size_4k_max_gb),
                (tc.KEY_1080_MIN, size_1080_min_gb), (tc.KEY_1080_MAX, size_1080_max_gb),
            ):
                if val is not None:
                    repo.set_setting(s, key, str(round(val, 2)))
            for name, key in keymap.items():
                if name in clear_keys:
                    repo.set_setting(s, key, "")   # 空 = 不限
            s.commit()
            return _settings_dict(s)
        finally:
            s.close()
    return await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# 手动检查(后台)
# ---------------------------------------------------------------------------
@router.post("/check")
async def trigger_check(cfg: dict = Depends(get_config)):
    """后台跑一次全量追踪检查。立即返回 job id, 前端轮询 status。"""
    job_id = uuid.uuid4().hex[:12]

    def _worker():
        with _JOBS_LOCK:
            _JOBS[job_id]["status"] = "running"
        logs = []

        def _log(*a):
            msg = " ".join(str(x) for x in a)
            logs.append(msg)

        try:
            res = tc.run(cfg, log=_log)
            with _JOBS_LOCK:
                _JOBS[job_id].update(status="done", result=res, logs=logs)
        except Exception as e:  # noqa: BLE001
            with _JOBS_LOCK:
                _JOBS[job_id].update(status="error", error=str(e)[:500], logs=logs)

    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "pending", "result": None, "error": None,
                         "started_at": time.time(), "logs": []}
    threading.Thread(target=_worker, name=f"track-check-{job_id}", daemon=True).start()
    return {"ok": True, "job": job_id}


@router.get("/check/status")
async def check_status(job: str = Query(...)):
    with _JOBS_LOCK:
        j = _JOBS.get(job)
    if j is None:
        raise HTTPException(404, "job 不存在或已过期")
    return {"job": job, "status": j["status"],
            "result": j.get("result"), "error": j.get("error"),
            "logs": (j.get("logs") or [])[-30:]}
