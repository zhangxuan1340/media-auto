#!/usr/bin/env python3
"""作业注册表 + cron 引擎 —— Seerr `server/job/schedule.ts` / `server/lib/settings.ts` 的移植

Seerr 的做法(本模块对齐它):
  · **一张统一的作业表**: 每个维护任务都在 `scheduledJobs` 里登记一条
    {id, name, type, interval, cronSchedule, running, cancelFn}, 而不是散落的定时器。
    媒体服务器相关只占两条 —— `jellyfin-recently-added-scan`(最近新增) 与
    `jellyfin-full-scan`(全库); 可用性对账是独立的 `availability-sync`。
  · **周期可编辑**: cron 存在 settings.json 的 `jobs.<id>.schedule`, 页面可改
    (`POST /jobs/:id/schedule`), 改完立即重排下一次执行。
  · **手动运行不改变时间表**: `job.invoke()` 只触发一次, 原来排好的下次时间不动。

本模块只放"元数据 + cron 计算 + 周期持久化", 保持无重依赖(不 import server/scripts),
这样 CLI 与 server 都能复用; 真正的执行函数在 `server/scheduler.py` 里按 id 映射。

cron 形式: 与 Seerr 一致用 6 段 `秒 分 时 日 月 周`(node-schedule 风格), 例如
`0 */5 * * * *` = 每 5 分钟、`0 0 3 * * *` = 每日 03:00。也接受标准 5 段(分 时 日 月 周)。
⚠️ 调度器按分钟粒度跑, 所以秒段只接受 `0`(或省略) —— 其它值视为非法, 免得出现
"写了却永远不触发"的假象。
"""
import json
import os

from lib.config import config_path, load_config

# ---------------------------------------------------------------------------
# 作业类型 / 周期单位(对齐 Seerr 的 ScheduledJob.type / interval)
# ---------------------------------------------------------------------------
TYPE_PROCESS = "process"      # 程序: 进程内跑的业务逻辑
TYPE_COMMAND = "command"      # 命令: 轻量例行命令(Seerr 用于下载同步)

UNIT_SECONDS = "seconds"
UNIT_MINUTES = "minutes"
UNIT_HOURS = "hours"
UNIT_DAYS = "days"

# ---------------------------------------------------------------------------
# 作业注册表
# ---------------------------------------------------------------------------
# schedule 为 Seerr 默认 cron; 用户可在管理页「作业」里改, 改后写进 config.json 的
# jobs.<id>.schedule 覆盖默认值(删掉该键即回落默认)。
JOBS = [
    {
        "id": "jellyfin-recently-added-scan",
        "name": "Jellyfin 最近新增扫描",
        "type": TYPE_PROCESS,
        "interval": UNIT_MINUTES,
        "schedule": "0 */5 * * * *",
        "desc": "拉 Jellyfin 最近新增的作品与分集, 并推导本地可用性。日常靠它保持同步。",
    },
    {
        "id": "jellyfin-full-scan",
        "name": "Jellyfin 全库扫描",
        "type": TYPE_PROCESS,
        "interval": UNIT_HOURS,
        "schedule": "0 0 3 * * *",
        "desc": "全库重建条目镜像与分集明细, 兜底「老剧新增集」与增量漏掉的条目。",
    },
    {
        "id": "availability-sync",
        "name": "同步媒体可用性",
        "type": TYPE_PROCESS,
        "interval": UNIT_HOURS,
        "schedule": "0 0 5 * * *",
        "desc": "对账本地可用性与 Jellyfin 实际内容, 把已从磁盘删除的作品/季标记为出库。",
    },
    {
        "id": "tmdb-sync",
        "name": "TMDB 元数据同步",
        "type": TYPE_PROCESS,
        "interval": UNIT_HOURS,
        "schedule": "0 30 3 * * *",
        "desc": "按 Jellyfin 库拉取 TMDB 详情/演员/分集结构到本地缓存。",
    },
    {
        "id": "track-check",
        "name": "追踪检查",
        "type": TYPE_PROCESS,
        "interval": UNIT_HOURS,
        "schedule": "0 0 */6 * * *",
        "desc": "检查已追踪演员的新作 / 剧集新季, 按设置自动推送磁力。",
    },
    {
        "id": "image-cache-cleanup",
        "name": "清理图片缓存",
        "type": TYPE_PROCESS,
        "interval": UNIT_HOURS,
        "schedule": "0 0 4 * * *",
        "desc": "清空本地图片缓存(下次浏览按需重新拉取)。",
    },
]

JOBS_BY_ID = {j["id"]: j for j in JOBS}


# ---------------------------------------------------------------------------
# cron 引擎(6 段: 秒 分 时 日 月 周 / 5 段: 分 时 日 月 周)
# ---------------------------------------------------------------------------
# 每段取值范围(下标与 _FIELDS 对齐: 分 时 日 月 周)
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


def _parse_field(expr, lo, hi):
    """解析一段 cron。支持 `*` `*/n` `a` `a-b` `a-b/n` 及逗号组合。

    返回 (取值集合, 是否为通配) —— 集合为 None 表示非法。
    "是否通配" 用于日/周的经典 OR 语义判定。
    """
    expr = (expr or "").strip()
    if not expr:
        return None, False
    if expr == "*":
        return set(range(lo, hi + 1)), True
    out = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            return None, False
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) <= 0:
                return None, False
            step = int(step_s)
            if not part:
                part = "*"
        if part == "*":
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            if not a.lstrip("-").isdigit() or not b.isdigit():
                return None, False
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None, False
        if start < lo or end > hi or start > end:
            return None, False
        out.update(range(start, end + 1, step))
    return (out or None), False


def parse_cron(expr):
    """校验并归一化 cron。

    返回 (分, 时, 日, 月, 周, 日是否通配, 周是否通配) 或 None(非法)。
    """
    if not isinstance(expr, str):
        return None
    parts = expr.split()
    if len(parts) == 6:
        sec, rest = parts[0], parts[1:]
        # 调度器按分钟粒度, 秒段只接受 0(或不写)—— 别的值会"永远不触发"
        if sec not in ("0",):
            return None
    elif len(parts) == 5:
        rest = parts
    else:
        return None
    parsed = []
    for i, raw in enumerate(rest):
        s, wild = _parse_field(raw, *_RANGES[i])
        if s is None:
            return None
        if i in (2, 4):          # 日 / 周 需要知道是否通配(OR 语义)
            wild = raw.strip() == "*"
        parsed.append((s, wild))
    (mi, _), (ho, _), (dom, dom_wild), (mo, _), (dow, dow_wild) = parsed
    return mi, ho, dom, mo, dow, dom_wild, dow_wild


def valid_cron(expr) -> bool:
    return parse_cron(expr) is not None


def next_run(expr, after):
    """下一次触发时刻(严格晚于 after)。非法 cron 返回 None。

    按分钟向前扫描(最多一年)。日/周都用限定时按经典 cron 的 OR 语义 ——
    只写 `0 0 1 * 1` 是"每月 1 号**或**每周一", 而不是"两者都满足"。
    """
    from datetime import datetime, timedelta
    p = parse_cron(expr)
    if p is None:
        return None
    mi, ho, dom, mo, dow, dom_wild, dow_wild = p
    # 归到整分, 再前进一分钟 ⇒ 结果严格 > after
    cur = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cur + timedelta(days=366)
    while cur < limit:
        if cur.minute in mi and cur.hour in ho and cur.month in mo:
            dow_hit = ((cur.weekday() + 1) % 7) in dow      # cron: 0=周日
            dom_hit = cur.day in dom
            if dom_wild and dow_wild:
                return cur
            if dom_wild:
                if dow_hit:
                    return cur
            elif dow_wild:
                if dom_hit:
                    return cur
            elif dom_hit or dow_hit:                        # 两者都限定 → OR
                return cur
        cur += timedelta(minutes=1)
    return None


# ---------------------------------------------------------------------------
# 周期持久化(config.json 的 jobs.<id>.schedule, 与 Seerr settings.json 同构)
# ---------------------------------------------------------------------------
def default_schedule(job_id):
    j = JOBS_BY_ID.get(job_id)
    return j["schedule"] if j else None


def get_schedule(cfg, job_id):
    """取作业当前周期: config.json 覆盖值优先, 否则用注册表默认值。"""
    ov = ((cfg or {}).get("jobs") or {}).get(job_id) or {}
    sched = (ov.get("schedule") or "").strip()
    if sched and valid_cron(sched):
        return sched
    return default_schedule(job_id)


def get_all_schedules(cfg):
    return {j["id"]: get_schedule(cfg, j["id"]) for j in JOBS}


def set_schedule(job_id, expr):
    """把作业周期写回 config.json(原子替换, 保留文件其余内容与键序)。

    ⚠️ 若写入值恰好等于注册表默认值, 则**删掉覆盖项**而不是写一条同值记录 ——
    这样 `isScheduleCustom` 与"是否真的被改过"始终一致, config 也不会越攒越脏。

    返回 True 表示配置确实发生了变化; 非法 cron 抛 ValueError。
    """
    if job_id not in JOBS_BY_ID:
        raise KeyError(job_id)
    expr = (expr or "").strip()
    if not valid_cron(expr):
        raise ValueError(f"无效的作业周期: {expr!r}(需 6 段 cron, 如 `0 */5 * * * *`)")
    p = config_path()
    data = load_config()
    jobs_sec = data.get("jobs") or {}
    cur = ((jobs_sec.get(job_id) or {}).get("schedule") or "").strip()
    if cur == expr:
        return False
    jobs_sec.setdefault(job_id, {})
    if expr == default_schedule(job_id):
        jobs_sec.pop(job_id, None)              # 回默认 → 去掉覆盖
        if not jobs_sec:
            data.pop("jobs", None)
        else:
            data["jobs"] = jobs_sec
    else:
        jobs_sec[job_id]["schedule"] = expr
        data["jobs"] = jobs_sec
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, p)
    return True


def _uniform_step(values):
    """取值集合若是"从最小值起等步长"的序列, 返回该步长, 否则 None。

    {0,5,...,55} → 5;  {0,6,12,18} → 6;  {0,3} → 3;  其它 → None。
    """
    vals = sorted(values)
    if len(vals) < 2:
        return None
    d = vals[1] - vals[0]
    if d <= 1:
        return None
    for a, b in zip(vals, vals[1:]):
        if b - a != d:
            return None
    return d


def describe_cron(expr) -> str:
    """把 cron 说成人话(给前端做兜底提示, 前端另有相对时间显示)。"""
    p = parse_cron(expr)
    if p is None:
        return "无效周期"
    mi, ho, dom, mo, dow, dom_wild, dow_wild = p
    all_days = mo == set(range(1, 13)) and dom_wild and dow_wild
    # 每日固定时刻: 单个小时 + 单个分钟
    if all_days and len(ho) == 1 and len(mi) == 1:
        return f"每日 {next(iter(ho)):02d}:{next(iter(mi)):02d}"
    # 每 N 分钟(小时内等步长, 跨全部小时)
    if all_days and len(ho) == 24:
        if mi == {0}:
            return "每小时整点"
        d = _uniform_step(mi)
        if d:
            return f"每 {d} 分钟"
        return "每小时"
    # 每 N 小时(分钟固定, 跨全部天)
    if all_days:
        d = _uniform_step(ho)
        if d and len(mi) == 1:
            return f"每 {d} 小时"
    # 非全天的: 交给前端显示原文
    return expr

