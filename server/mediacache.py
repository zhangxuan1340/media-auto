"""MediaAuto 展示元数据 —— "实时查 TMDB + 进程内缓存"
================================================================
作品**清单/可用性**来自 media/season 表(全量权威)。
展示元数据(标题/年份/海报/类型)不落库做权威源, 而是:
    进程内缓存(TTL) → (可选)tmdb_media 快速路径 → TMDB 实时
元数据不落库为权威源, 进程内缓存 + 重启可重建。
"""
import asyncio
import time

TTL = 24 * 3600
FETCH_CONCURRENCY = 8   # 并行拉 TMDB(限速 ~10/s, 8 并发安全)

_cache: dict = {}       # {(kind, tmdb_id): (ts, payload)} —— 见 _put, 有上限
_CACHE_MAX = 512        # 上限: 展示元数据是小 dict, 但长期翻页会涨到全库(6000+)条


def _put(key, payload):
    """写缓存并控制上限(旧写法无上限, 进程活得越久占得越多)。
    满了就淘汰最旧的一条 —— 读远多于写, O(n) 扫一次可接受;
    被淘汰的下次会先走 tmdb_media 本地快路(毫秒级), 不会真的去打 TMDB。"""
    if len(_cache) >= _CACHE_MAX and key not in _cache:
        try:
            old = min(_cache.items(), key=lambda kv: kv[1][0])
            _cache.pop(old[0], None)
        except ValueError:
            pass
    _cache[key] = (time.time(), payload)


def _payload_from_tmdb(meta: dict) -> dict:
    return {
        "title": meta.get("title", ""),
        "originalTitle": meta.get("originalTitle", ""),
        "year": meta.get("year", ""),
        "overview": (meta.get("overview", "") or "")[:300],
        "poster": meta.get("poster", ""),
        "backdrop": meta.get("backdrop", ""),
        "vote": meta.get("vote", 0.0),
        "genres": list(meta.get("genres") or []),
        "in_production": bool(meta.get("in_production")),
        "status": meta.get("status", ""),
        "imdb_id": meta.get("imdb_id", ""),
        "runtime": meta.get("runtime") or 0,
        "premiered": meta.get("premiered", ""),
        "certification": meta.get("certification", ""),
    }


async def get_display(cfg, kind: str, tmdb_id: int, session=None) -> dict:
    """取一部作品的展示元数据。
    顺序: 进程内缓存 → tmdb_media 快速路径(本地读, 秒回) → TMDB 实时(回填缓存)。
    全失败返回占位(标题=TMDB ID, 前端可识别)。"""
    key = (kind, tmdb_id)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]

    # 快速路径: 老元数据缓存表(若仍在)本地读
    if session is not None:
        try:
            from db import repositories as repo
            row = repo.get_tmdb_media_by_id(session, kind, tmdb_id)
            if row is not None and (row.title or row.year or row.poster):
                p = {
                    "title": row.title or "", "originalTitle": row.original_title or "",
                    "year": row.year or "", "overview": (row.overview or "")[:300],
                    "poster": row.poster or "", "backdrop": row.backdrop or "",
                    "vote": row.vote or 0.0,
                    "genres": [g for g in (row.genre_names or "").split(",") if g],
                    "in_production": bool(row.in_production), "status": row.status or "",
                    "imdb_id": row.imdb_id or "", "runtime": row.runtime or 0,
                    "premiered": row.premiered or "", "certification": row.certification or "",
                }
                _put(key, p)
                return p
        except Exception:  # noqa: BLE001  表不存在等
            pass

    # 实时 TMDB
    try:
        from clients.tmdb import client as tmdb
        meta = None
        for _ in range(2):
            try:
                meta = await tmdb.detail(cfg, kind, tmdb_id)
                if meta:
                    break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.8)
        if meta:
            p = _payload_from_tmdb(meta)
            _put(key, p)
            return p
    except Exception:  # noqa: BLE001
        pass
    return {"title": f"#{tmdb_id}", "originalTitle": "", "year": "", "overview": "",
            "poster": "", "backdrop": "", "vote": 0.0, "genres": [],
            "in_production": False, "status": "", "imdb_id": "", "runtime": 0,
            "premiered": "", "certification": ""}


async def get_many(cfg, kind: str, tmdb_ids, session=None) -> dict:
    """并行取一批展示元数据 → {tmdb_id: payload}。"""
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(tid):
        async with sem:
            return tid, await get_display(cfg, kind, tid, session)

    res = {}
    for tid, p in await asyncio.gather(*[one(t) for t in tmdb_ids]):
        res[tid] = p
    return res


def stats() -> dict:
    return {"cached": len(_cache)}
