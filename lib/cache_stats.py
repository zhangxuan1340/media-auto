#!/usr/bin/env python3
"""缓存统计 —— 管理页「作业与缓存」下半张表的后端

每个缓存都暴露 {hits, misses, keys, ksize, vsize},
页面上就是「缓存名 | 击中数 | 失误数 | 键数 | 键储存大小 | 值储存大小」+「清除缓存」。

**统计是进程内的**(重启归零) —— hits/misses 来自内存计数器。
键数与体积则取自真实存储, 所以重启后仍然准。

MediaAuto 实际存在的两处缓存:
  · `tmdb`  — TMDB API 元数据缓存。命中 = 没走网络(本地 tmdb_media 行 / 进程内 TTL 缓存);
              失误 = 真的向 TMDB 发了请求。
  · `image` — 图片本地缓存(`data/img_cache/<sha1>.bin|.meta`, 见 server/imgproxy.py)。
              命中 = 本地有文件直接返回; 失误 = 回源拉取并落盘。
"""
import threading

# 缓存 id → 展示名
CACHES = {
    "tmdb": "The Movie Database API",
    "image": "The Movie Database (tmdb) 图片",
}

_lock = threading.Lock()
_stats = {cid: {"hits": 0, "misses": 0} for cid in CACHES}


def hit(cache_id, n=1):
    """记一次命中(未走网络/未回源)。未知缓存 id 静默忽略, 免得埋点处要处处守卫。"""
    with _lock:
        s = _stats.get(cache_id)
        if s is not None:
            s["hits"] += n


def miss(cache_id, n=1):
    """记一次失误(实际请求了上游)。"""
    with _lock:
        s = _stats.get(cache_id)
        if s is not None:
            s["misses"] += n


def reset(cache_id=None):
    """清零计数器(清空缓存时一并归零, 让页面数字与存储同步)。"""
    with _lock:
        for cid in (CACHES if cache_id is None else [cache_id]):
            if cid in _stats:
                _stats[cid] = {"hits": 0, "misses": 0}


def _counters(cache_id):
    with _lock:
        return dict(_stats.get(cache_id, {"hits": 0, "misses": 0}))


# ---------------------------------------------------------------------------
# 各缓存的存储实况(键数 / 键体积 / 值体积)
# ---------------------------------------------------------------------------
def _tmdb_store():
    """TMDB 元数据缓存 = `tmdb_media`(作品) + `tmdb_season`(季/分集结构)。

    键数取两者行数之和; 值体积 ≈ 各文本列的字节总和。
    """
    from db.database import SessionLocal
    from sqlalchemy import func, select, cast, String
    from db.models import TmdbMedia, TmdbSeason
    s = SessionLocal()
    try:
        med_expr = (func.length(func.coalesce(TmdbMedia.title, ""))
                    + func.length(func.coalesce(TmdbMedia.overview, ""))
                    + func.length(func.coalesce(TmdbMedia.cast_json, ""))
                    + func.length(func.coalesce(TmdbMedia.directors_json, ""))
                    + func.length(func.coalesce(TmdbMedia.studios_json, ""))
                    + func.length(func.coalesce(TmdbMedia.keywords_json, ""))
                    + func.length(func.coalesce(TmdbMedia.genre_names, "")))
        m_keys, m_vsize = s.execute(
            select(func.count(TmdbMedia.id), func.coalesce(func.sum(med_expr), 0))).one()
        sea_expr = (func.length(func.coalesce(TmdbSeason.name, ""))
                    + func.length(func.coalesce(TmdbSeason.episode_numbers, "")))
        s_keys, s_vsize = s.execute(
            select(func.count(TmdbSeason.id), func.coalesce(func.sum(sea_expr), 0))).one()
        # 键长: tmdb_id 数字位数 + kind 长度(近似)
        ksize = s.execute(select(func.coalesce(func.sum(
            func.length(cast(TmdbMedia.tmdb_id, String)) + func.length(TmdbMedia.kind)), 0))).one()[0]
        return (int(m_keys or 0) + int(s_keys or 0),
                int(ksize or 0),
                int(m_vsize or 0) + int(s_vsize or 0))
    except Exception as e:  # noqa: BLE001
        # 不静默: 统计出错不该把真实行数显示成 0 而没人发现
        print(f"[cache] 读取 TMDB 缓存存储失败: {e}", flush=True)
        return 0, 0, 0
    finally:
        s.close()


def _img_store():
    """图片缓存: 键 = .bin 文件, 值体积 = 目录内所有文件字节和。"""
    import os
    from lib.config import project_root
    d = os.path.join(project_root(), "data", "img_cache")
    if not os.path.isdir(d):
        return 0, 0, 0
    keys = 0
    total = 0
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            total += st.st_size
            if name.endswith(".bin"):
                keys += 1
    except OSError:
        return 0, 0, 0
    return keys, 0, total          # 图片无独立"键体积", 记 0


def _flush_tmdb():
    """清空 TMDB 元数据缓存。

    ⚠️ 这会清掉浏览/缺失页依赖的本地元数据 —— 之后由「TMDB 元数据同步」作业或
    浏览时的按需现拉逐步补回(`_pull_detail` 不写库, 补回靠同步作业)。
    """
    from db.database import SessionLocal
    from db.models import TmdbMedia
    s = SessionLocal()
    try:
        n = s.query(TmdbMedia).delete(synchronize_session=False)
        s.commit()
        return int(n or 0)
    finally:
        s.close()


def _flush_image():
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


_FLUSHERS = {"tmdb": _flush_tmdb, "image": _flush_image}


def flush(cache_id):
    """清空指定缓存。返回删除的条目数; 未知 id 返回 None。"""
    fn = _FLUSHERS.get(cache_id)
    if fn is None:
        return None
    n = fn()
    reset(cache_id)
    return n


def snapshot():
    """返回 {apiCaches, imageCache} 快照(前端缓存表的数据源)。"""
    rows = []
    store = {"tmdb": _tmdb_store, "image": _img_store}
    for cid, name in CACHES.items():
        c = _counters(cid)
        keys, ksize, vsize = store[cid]()
        rows.append({
            "id": cid,
            "name": name,
            "stats": {
                "hits": c["hits"],
                "misses": c["misses"],
                "keys": keys,
                "ksize": ksize,
                "vsize": vsize,
            },
        })
    return rows
