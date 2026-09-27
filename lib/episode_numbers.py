"""TMDB 集号回填 —— 分集级缺失对比的权威基准。

`tmdb_season.episode_numbers` 存 TMDB 每季真实 `episode_number` 列表, **默认是空的**
(TMDB 同步只写 `episode_count`)。缺它就只能按 `1..count` 估算, 而 TMDB 对长篇动画用
**跨季连续的绝对集号** —— 《火影忍者:疾风传》(31910) S02=33..53 … S20=414..500,
本地 Jellyfin 同源亦然 → 估算的 1..count 与实有集号完全不相交,
整部剧被误报成「缺468 多468」(2026-09-27 用户反馈; Seerr 拉真实集号所以显示完整)。

因此缺失对比的规矩是: **没有真实集号就不猜**(`browse._series_missing` 里
「未回填 + 兜底不适用 → missingCount=None / 编号未同步」), 本模块负责把真实集号补齐:

  · hydrate_show       单剧并发回填空季并落库(一次回填永久缓存, 失败 600s 退避)
  · hydrate_many       批量(后台补: TMDB 同步收尾 / 缺失页后台任务)
  · persist_season_numbers  展开某季时的顺手落库(0 额外 TMDB 请求)
  · shows_needing_hydration  挑出"集号还空着"的在库剧(稳态返回空, 0 请求)
"""
import asyncio
import json
import time

from db.database import SessionLocal
from db import repositories as repo

_BACKOFF: dict = {}          # {tmdb_id: ts} 回填失败退避
BACKOFF_SECONDS = 600        # TMDB 挂了别把每个请求都变成重试风暴
_SEASON_CONCURRENCY = 3      # TMDB 限速 ~10 req/s(与 sync_tmdb 的 5 并发同量级); 后台批量回填时更收敛些


def _pending_seasons(tmdb_id, session=None):
    """该剧中还没集号的季号。自带 session 或复用调用方的。"""
    own = session is None
    s = SessionLocal() if own else session
    try:
        return [se.season_number for se in repo.get_tmdb_seasons(s, tmdb_id)
                if not se.episode_numbers]
    finally:
        if own:
            s.close()


def shows_needing_hydration(tmdb_ids) -> list:
    """一批剧里仍需回填的那些。

    **一条 SQL** 拿到"有空集号的剧"(逐剧开 session 查会在大库上拖成 2200 次查询),
    再剔掉失败退避中的。无网络; 全部回填过的库返回空列表 → 0 开销。
    """
    ids = []
    for tid in tmdb_ids or ():
        try:
            ids.append(int(tid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    from db.models import TmdbSeason
    s = SessionLocal()
    try:
        rows = (s.query(TmdbSeason.tmdb_id)
                .filter(TmdbSeason.tmdb_id.in_(ids))
                .filter(TmdbSeason.episode_numbers == "")
                .distinct()
                .all())
        need = {int(r[0]) for r in rows}
    finally:
        s.close()
    now = time.time()
    return [t for t in sorted(need)
            if not (_BACKOFF.get(t) and now - _BACKOFF[t] < BACKOFF_SECONDS)]


async def hydrate_show(cfg, tmdb_id) -> int:
    """回填单剧空季的集号并落库, 返回本次写入的季数。

    并发拉取(信号量限流); 失败不抛 —— 上层退到「编号未同步」, 不用估算数字。
    """
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        return 0
    if _BACKOFF.get(tmdb_id) and time.time() - _BACKOFF[tmdb_id] < BACKOFF_SECONDS:
        return 0
    pend = await asyncio.to_thread(_pending_seasons, tmdb_id)
    if not pend:
        return 0
    from clients.tmdb import client as tmdb
    sem = asyncio.Semaphore(_SEASON_CONCURRENCY)

    async def _one(sn):
        async with sem:
            eps = await tmdb.season_episodes(cfg, tmdb_id, sn)
        return sn, [e.get("episode") for e in (eps or []) if e.get("episode") is not None]

    try:
        got = await asyncio.gather(*(_one(sn) for sn in pend))
    except Exception:  # noqa: BLE001
        _BACKOFF[tmdb_id] = time.time()
        return 0
    filled = {sn: nums for sn, nums in got if nums}
    if len(filled) < len(pend):
        _BACKOFF[tmdb_id] = time.time()   # 有季没拉到 → 整体稍后重试
    if not filled:
        return 0

    def _save():
        s = SessionLocal()
        try:
            n = 0
            for se in repo.get_tmdb_seasons(s, tmdb_id):
                if se.season_number in filled and not se.episode_numbers:
                    se.episode_numbers = json.dumps(filled[se.season_number])
                    n += 1
            if n:
                s.commit()
            return n
        finally:
            s.close()

    return await asyncio.to_thread(_save)


async def hydrate_many(cfg, tmdb_ids) -> int:
    """逐剧回填(剧内已并发)。返回总写入季数; 单剧失败不影响其他剧。"""
    total = 0
    for tid in tmdb_ids:
        try:
            total += await hydrate_show(cfg, int(tid))
        except Exception:  # noqa: BLE001
            continue
    return total


def persist_season_numbers(tmdb_id, season_number, nums) -> int:
    """把一季的真实集号落库(展开分集明细时的副产品, 不额外打 TMDB)。"""
    if not nums:
        return 0
    s = SessionLocal()
    try:
        from db.models import TmdbSeason
        se = (s.query(TmdbSeason)
              .filter_by(tmdb_id=tmdb_id, season_number=season_number).first())
        if se and not se.episode_numbers:
            se.episode_numbers = json.dumps(nums)
            s.commit()
            return 1
        return 0
    except Exception:  # noqa: BLE001
        s.rollback()
        return 0
    finally:
        s.close()
