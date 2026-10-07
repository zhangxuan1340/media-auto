"""MediaAuto 进程内调度器
==============================================================
不再是"每个任务一段硬编码的到期判断", 而是**遍历统一的作业注册表**(`lib/jobs.py`),
已登记的作业:

    作业名                 类型      默认周期        映射
    Jellyfin 最近新增扫描   process   每 5 分钟       A 层条目同步 + 分集增量 + 可用性推导
    Jellyfin 全库扫描       process   每日 03:00      全库重建条目/分集
    同步媒体可用性          process   每日 05:00      对账并标记出库
    TMDB 元数据同步         process   每日 03:30      按库拉 TMDB 详情
    追踪检查                process   每 6 小时       演员新作/新季 → 推磁力
    清理图片缓存            process   每日 04:00      清空本地图片缓存

三条关键语义:
  1. **周期可编辑**: cron 存数据库 `jobs.<id>.schedule`, 页面改完立即重排。
  2. **手动运行不改变时间表**: 手动只触发一次, 已排好的"下一次执行"保持不动。
  3. **下一次执行时间可查询**: 由 `next_run(cron, last_run)` 实时推导。

宕机补跑: next_run 是从 **上次自动运行的时刻** 往后推的, 所以进程停了一段时间再起来,
落在过去的 next_run 会让该作业在下一拍立即补跑一次(而不是白等一整天) ——
这正是本地媒体工具最想要的语义。

每个作业: 写一条 sync_log(running → success/error), 单个作业失败不影响其它作业与调度器。
"""
import threading
import time
from datetime import datetime

from db.database import SessionLocal, init_db
from db import repositories as repo
from lib import jobs as reg

TICK_SEC = 20                       # 调度器轮询间隔
KEY_LAST_PREFIX = "job_last_run:"   # sync_state: job_last_run:<id> → 上次自动运行时刻

# 旧调度器遗留的键(仅用于首次迁移, 避免升级后重复跑一遍当天的全量/对账)
_LEGACY_KEYS = {
    "jellyfin-recently-added-scan": ("scheduler_recent_scan_at", "iso"),
    "jellyfin-full-scan": ("scheduler_full_scan_date", "date@03:00"),
    "availability-sync": ("scheduler_availability_date", "date@05:00"),
    "track-check": ("scheduler_track_check_at", "iso"),
}

# 作业 → sync_log 的"来源"列(scope 列写作业 id)
_SOURCES = {
    "jellyfin-recently-added-scan": "jellyfin",
    "jellyfin-full-scan": "jellyfin",
    "availability-sync": "jellyfin",
    "tmdb-sync": "tmdb",
    "track-check": "track",
    "image-cache-cleanup": "system",
}

_runtime = {}          # id -> {running, last_run, next_run, last_status, last_error}
_rt_lock = threading.RLock()
_thread = None
_started = False


# ---------------------------------------------------------------------------
# 作业实现(每个返回 int = 处理条数, 供 sync_log.items_synced)
# ---------------------------------------------------------------------------
def _sync_jellyfin_items():
    """先刷新 A 层(jellyfin_item 条目镜像)。

    可用性扫描(B 层 media/season)依赖 A 层的 `item_id → tmdb_id` 映射来聚合实有集;
    A 层不更新, 新入库的剧就会因为"查不到映射 → 实有集算成 0"而卡在 UNKNOWN。
    这一步很轻(只按游标拉新增条目), 失败也不阻断后续扫描。
    """
    try:
        from lib.loop_clients import run_coro
        from scripts import sync_jellyfin as sj
        run_coro(sj.run(scope="items", full=False))
    except Exception:  # noqa: BLE001
        pass


def _job_recently_added():
    """Jellyfin 最近新增扫描: A 层条目同步 + recent 扫描(含分集增量/可用性推导)。

    两步都写媒体库, 整体持锁当一个原子单元 —— 可用性扫描依赖 A 层刚刷新的
    item_id→tmdb_id 映射, 中间被别的任务插进来改 media 会互相覆盖。
    """
    from lib.sync_guard import SYNC_LOCK
    from scripts import sync_jf_scanner as s
    with SYNC_LOCK:                      # 可重入, 内部 sj.run/s.run 再取同锁不会自锁
        _sync_jellyfin_items()
        stats = s.run(mode="recent", limit=0)
    return int(stats.get("movie", 0) + stats.get("show", 0))


def _job_full_scan():
    from lib.sync_guard import SYNC_LOCK
    from scripts import sync_jf_scanner as s
    with SYNC_LOCK:
        _sync_jellyfin_items()
        stats = s.run(mode="full", limit=0)
    return int(stats.get("movie", 0) + stats.get("show", 0))


def _job_availability():
    from lib.sync_guard import SYNC_LOCK
    from scripts import availability_sync as s
    with SYNC_LOCK:
        stats = s.run(dry=False)
    return int(stats.get("movie_deleted", 0) + stats.get("show_deleted", 0)
               + stats.get("seasons_deleted", 0))


def _job_tmdb_sync():
    """按 Jellyfin 库增量拉取 TMDB 元数据(缺什么补什么, 不重刷未过期的)。"""
    from lib.loop_clients import run_coro
    from scripts import sync_tmdb as s
    return int(run_coro(s.run("all", full=False)) or 0)


def _job_track_check():
    """追踪检查(演员新作/剧集新季 → 按设置自动推磁力)。返回新增+推送合计。"""
    from scripts import track_check as s
    res = s.run()
    return int(res.get("new_total", 0) + res.get("pushed", 0))


def _job_image_cache_cleanup():
    """清空本地图片缓存, 返回删除的文件数(.bin/.meta 成对删)。"""
    import os
    from lib.config import project_root
    d = os.path.join(project_root(), "data", "img_cache")
    if not os.path.isdir(d):
        return 0
    n = 0
    for name in os.listdir(d):
        if not (name.endswith(".bin") or name.endswith(".meta")):
            continue
        try:
            os.remove(os.path.join(d, name))
            n += 1
        except OSError:
            pass
    return n


_RUNNERS = {
    "jellyfin-recently-added-scan": _job_recently_added,
    "jellyfin-full-scan": _job_full_scan,
    "availability-sync": _job_availability,
    "tmdb-sync": _job_tmdb_sync,
    "track-check": _job_track_check,
    "image-cache-cleanup": _job_image_cache_cleanup,
}


# ---------------------------------------------------------------------------
# 时间表: last_run 持久化 + next_run 推导
# ---------------------------------------------------------------------------
def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:  # noqa: BLE001
        return None


def _persist_last_run(job_id, when):
    s = SessionLocal()
    try:
        repo.set_sync_state(s, KEY_LAST_PREFIX + job_id, when.isoformat())
        s.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        s.close()


def _load_last_run(session, job_id):
    return _parse_ts(repo.get_sync_state(session, KEY_LAST_PREFIX + job_id))


def _seed_from_legacy(session, job_id):
    """首次升级: 把旧调度器的状态键翻译成新作业的 last_run, 避免重复跑当天任务。"""
    spec = _LEGACY_KEYS.get(job_id)
    if not spec:
        return None
    key, kind = spec
    raw = repo.get_sync_state(session, key)
    if not raw:
        return None
    if kind == "iso":
        return _parse_ts(raw)
    try:                                    # date@HH:MM
        d = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None
    hh, mm = (int(x) for x in kind.split("@")[1].split(":"))
    return d.replace(hour=hh, minute=mm)


def _compute_next(job_id, last_run, cfg=None):
    """下一次执行时刻 = cron 从"上次自动运行"往后推(没有则从当前时刻起算)。"""
    from server.config import get_config
    cfg = cfg if cfg is not None else get_config()
    sched = reg.get_schedule(cfg, job_id)
    base = last_run or datetime.now()
    return reg.next_run(sched, base)


def _snapshot(job_id, cfg=None):
    """作业对外快照(字段名见 GET /jobs)。"""
    from server.config import get_config
    cfg = cfg if cfg is not None else get_config()
    meta = reg.JOBS_BY_ID[job_id]
    with _rt_lock:
        rt = dict(_runtime.get(job_id, {}))
    cur = reg.get_schedule(cfg, job_id)
    nxt = rt.get("next_run")
    if nxt is None:
        nxt = _compute_next(job_id, rt.get("last_run"), cfg)
        with _rt_lock:
            if job_id in _runtime:
                _runtime[job_id]["next_run"] = nxt
    last = rt.get("last_run")
    return {
        "id": job_id,
        "name": meta["name"],
        "type": meta["type"],
        "interval": meta["interval"],
        "desc": meta["desc"],
        "cronSchedule": cur,
        "defaultSchedule": meta["schedule"],
        "isScheduleCustom": cur != meta["schedule"],
        "describe": reg.describe_cron(cur),
        "nextExecutionTime": nxt.isoformat() if nxt else None,
        "lastRunTime": last.isoformat() if last else None,
        "lastStatus": rt.get("last_status", ""),
        "lastError": rt.get("last_error", ""),
        "running": bool(rt.get("running")),
    }


def list_jobs(cfg=None):
    return [_snapshot(j["id"], cfg) for j in reg.JOBS]


def get_job(job_id, cfg=None):
    """单个作业快照(供 POST /jobs/:id/run 的回执)。未知 id 返回 None。"""
    if job_id not in reg.JOBS_BY_ID:
        return None
    return _snapshot(job_id, cfg)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
from db.models import SyncLog as _SyncLog  # noqa: E402


def _log_start(source, scope):
    s = SessionLocal()
    try:
        log = repo.log_sync_start(s, source, scope)
        s.commit()
        return log.id
    except Exception:  # noqa: BLE001
        return None
    finally:
        s.close()


def _log_end(log_id, status, total, err=""):
    if log_id is None:
        return
    s = SessionLocal()
    try:
        lg = s.get(_SyncLog, log_id)
        if lg is not None:
            repo.log_sync_end(s, lg, status, int(total or 0), err)
            s.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        s.close()


def _execute(job_id, manual=False):
    """真正跑一个作业。manual=True 时不推进时间表(手动运行不改变计划)。"""
    with _rt_lock:
        rt = _runtime.setdefault(job_id, {})
        if rt.get("running"):
            return False
        rt["running"] = True

    scope = f"{job_id}{'(手动)' if manual else '(自动)'}"
    log_id = _log_start(_SOURCES.get(job_id, "system"), scope)
    status, err, total = "success", "", 0
    t0 = time.time()
    try:
        total = int(_RUNNERS[job_id]() or 0)
    except Exception as e:  # noqa: BLE001
        status, err = "error", str(e)[:2000]
    finally:
        _log_end(log_id, status, total, err)
        with _rt_lock:
            rt["running"] = False
            rt["last_status"] = status
            rt["last_error"] = err
            if not manual:
                rt["last_run"] = datetime.now()
                _persist_last_run(job_id, rt["last_run"])
            try:
                rt["next_run"] = _compute_next(job_id, rt.get("last_run"))
            except Exception:  # noqa: BLE001
                pass
        print(f"[job] {job_id} {'手动' if manual else '自动'} {status} "
              f"{total} 条 {time.time() - t0:.0f}s"
              + (f" 错误: {err}" if err else ""), flush=True)
    return True


def start_job(job_id, manual=True):
    """按需触发一次(POST /jobs/:id/run)。返回 (True, 快照) / (False, 原因)。

    立即返回, 作业在后台线程里跑。
    """
    if job_id not in reg.JOBS_BY_ID:
        return False, "作业不存在"
    with _rt_lock:
        if _runtime.get(job_id, {}).get("running"):
            return False, "该作业正在运行中"
    threading.Thread(target=_execute, args=(job_id, manual), daemon=True,
                     name=f"job-{job_id}").start()
    return True, None


def reschedule_job(job_id, expr):
    """改周期(POST /jobs/:id/schedule)。非法 cron 抛 ValueError。"""
    if job_id not in reg.JOBS_BY_ID:
        raise KeyError(job_id)
    reg.set_schedule(job_id, expr)          # 非法则 ValueError
    from server.config import reload_config
    cfg = reload_config()
    with _rt_lock:
        rt = _runtime.setdefault(job_id, {})
        rt["next_run"] = _compute_next(job_id, rt.get("last_run"), cfg)
    return _snapshot(job_id, cfg)


# ---------------------------------------------------------------------------
# 调度循环
# ---------------------------------------------------------------------------
def _tick():
    """一拍: 遍历作业表, 到期的起线程跑(单个作业长跑不阻塞其它作业)。"""
    now = datetime.now()
    due = []
    with _rt_lock:
        for meta in reg.JOBS:
            job_id = meta["id"]
            rt = _runtime.setdefault(job_id, {
                "running": False, "last_run": None, "next_run": None,
                "last_status": "", "last_error": "",
            })
            if rt.get("running"):
                continue
            nxt = rt.get("next_run")
            if nxt is None:
                try:
                    nxt = _compute_next(job_id, rt.get("last_run"))
                except Exception:  # noqa: BLE001
                    nxt = None
                rt["next_run"] = nxt
            if nxt is not None and nxt <= now:
                due.append(job_id)
    for job_id in due:
        start_job(job_id, manual=False)


def _loop():
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001  调度器永不因单次异常退出
            pass
        time.sleep(TICK_SEC)


def _bootstrap_state():
    """启动时把各作业的 last_run 从 sync_state 读出来(含旧调度器键的迁移)。

    迁移的意义: 升级瞬间旧键里记着"今天已经跑过全量/对账", 若不搬到新键,
    新作业会认为从没跑过 → 起服后立刻重跑一遍当天的全量扫描(白干几十分钟)。
    """
    lasts = {}
    s = SessionLocal()
    try:
        for meta in reg.JOBS:
            job_id = meta["id"]
            last = _load_last_run(s, job_id)
            if last is None:
                last = _seed_from_legacy(s, job_id)
                if last is not None:
                    repo.set_sync_state(s, KEY_LAST_PREFIX + job_id, last.isoformat())
            lasts[job_id] = last
        s.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        s.close()
    with _rt_lock:
        for meta in reg.JOBS:
            job_id = meta["id"]
            rt = _runtime.setdefault(job_id, {
                "running": False, "last_run": None, "next_run": None,
                "last_status": "", "last_error": "",
            })
            rt["last_run"] = lasts.get(job_id)
            try:
                rt["next_run"] = _compute_next(job_id, rt["last_run"])
            except Exception:  # noqa: BLE001
                rt["next_run"] = None


def start():
    """启动调度器(幂等)。server startup 时调用一次。"""
    global _thread, _started
    if _started:
        return
    init_db()
    _bootstrap_state()
    _thread = threading.Thread(target=_loop, name="mediaauto-scheduler", daemon=True)
    _thread.start()
    _started = True


def is_running() -> bool:
    return _started and (_thread is not None) and _thread.is_alive()
