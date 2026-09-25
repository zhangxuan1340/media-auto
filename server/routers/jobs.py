"""作业与缓存路由 —— Seerr `Settings → Jobs & Cache` 的后端契约

对齐 Seerr `server/routes/settings/index.ts`:
  GET  /api/jobs                    列出全部作业(id/name/type/interval/cronSchedule/
                                    nextExecutionTime/running …)
  POST /api/jobs/{jobId}/run        手动执行一次(不改变已排好的时间表)
  POST /api/jobs/{jobId}/schedule   改周期(cron), 非法 → 400
  GET  /api/cache                  API 缓存统计(击中/失误/键数/体积)+ 图片缓存
  POST /api/cache/{cacheId}/flush   清空某个缓存

作业元数据与 cron 在 `lib/jobs.py`, 运行时状态与执行在 `server/scheduler.py`。
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from server.auth import require_auth
from server.config import get_config
from lib import jobs as reg
from lib import cache_stats

router = APIRouter(prefix="/api", tags=["jobs"], dependencies=[Depends(require_auth)])


class ScheduleBody(BaseModel):
    schedule: str


def _empty_stats():
    return {"hits": 0, "misses": 0, "keys": 0, "ksize": 0, "vsize": 0}


# ---------------------------------------------------------------------------
# 作业
# ---------------------------------------------------------------------------
@router.get("/jobs")
async def api_jobs(cfg: dict = Depends(get_config)):
    """全部作业(顺序与 lib/jobs.py 注册表一致)。"""
    from server import scheduler
    return await run_in_threadpool(scheduler.list_jobs, cfg)


@router.post("/jobs/{job_id}/run")
async def api_job_run(job_id: str, cfg: dict = Depends(get_config)):
    """手动执行一次。**不改变时间表** —— 这是 Seerr 的明确语义
    (手动运行任务不会改变它的时间表), 所以这里不推进 nextExecutionTime。
    """
    from server import scheduler
    if job_id not in reg.JOBS_BY_ID:
        raise HTTPException(404, "作业不存在")
    ok, err = scheduler.start_job(job_id, manual=True)
    if not ok:
        raise HTTPException(409, err or "该作业无法启动")
    return await run_in_threadpool(scheduler.get_job, job_id, cfg)


@router.post("/jobs/{job_id}/schedule")
async def api_job_schedule(job_id: str, body: ScheduleBody,
                           cfg: dict = Depends(get_config)):
    """改周期(6 段 cron, 如 `0 */5 * * * *`)。非法周期返回 400。"""
    from server import scheduler
    if job_id not in reg.JOBS_BY_ID:
        raise HTTPException(404, "作业不存在")
    try:
        return await run_in_threadpool(scheduler.reschedule_job, job_id, body.schedule)
    except ValueError as e:
        raise HTTPException(400, str(e))     # lib.jobs 已给出可读文案, 别再套一层前缀


@router.post("/jobs/{job_id}/reset")
async def api_job_reset(job_id: str, cfg: dict = Depends(get_config)):
    """恢复该作业的默认周期(注册表里的出厂值)。"""
    from server import scheduler
    if job_id not in reg.JOBS_BY_ID:
        raise HTTPException(404, "作业不存在")
    dflt = reg.default_schedule(job_id)
    try:
        return await run_in_threadpool(scheduler.reschedule_job, job_id, dflt)
    except ValueError as e:  # 注册表默认值非法属于代码 bug, 但别 500 到前端
        raise HTTPException(500, f"默认周期非法: {e}")


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
@router.get("/cache")
async def api_cache():
    """API 缓存统计 + 图片缓存(形状对齐 Seerr: apiCaches[] / imageCache.tmdb)。"""
    rows = await run_in_threadpool(cache_stats.snapshot)
    api_caches = [r for r in rows if r["id"] == "tmdb"]
    img = next((r for r in rows if r["id"] == "image"), None)
    return {
        "apiCaches": api_caches,
        "imageCache": {"tmdb": (img["stats"] if img else _empty_stats())},
    }


@router.post("/cache/{cache_id}/flush")
async def api_cache_flush(cache_id: str):
    """清空指定缓存。返回删除条目数(Seerr 返回 204, 这里回数量便于前端提示)。"""
    n = await run_in_threadpool(cache_stats.flush, cache_id)
    if n is None:
        raise HTTPException(404, "缓存不存在")
    return {"ok": True, "id": cache_id, "removed": n}
