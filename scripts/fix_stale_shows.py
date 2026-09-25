#!/usr/bin/env python3
"""用修复后的 _process_show 重新处理那 11 部"有集却被判缺失"的剧, 验证所有权兜底。

流程与 full 扫描一致: 加载 eps_index → 取 TMDB 季(404 现返回 []) → 调 _process_show。
直接写库(逐部 commit), 顺带验证 guard 是否把 UNKNOWN/PROCESSING 抬成 PARTIALLY。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import load_config
from clients.tmdb import client as tmdb
from db.database import SessionLocal, init_db
from db.models import Media, MediaType, MediaStatus
import scripts.sync_jf_scanner as sc

TMDB_IDS = [119017, 232766, 272104, 322512, 207148, 133547, 116498,
            115317, 202320, 15281, 596258]


async def main():
    init_db()
    cfg = load_config()
    session = SessionLocal()
    eps_index = sc._load_episode_index(session)
    series_to_tmdb, tmdb_to_series, eps_by_series = eps_index
    before = {}
    after = {}
    for tid in TMDB_IDS:
        m = session.query(Media).filter_by(tmdb_id=tid, media_type=MediaType.TV).first()
        before[tid] = m.status if m else None
        tseasons = await tmdb.all_seasons(cfg, tid) or []
        sids = tmdb_to_series.get(str(tid), set())
        sc._process_show(session, tid, seasons=tseasons,
                         eps_by_series=eps_by_series, series_ids=sids)
        session.commit()
        m2 = session.query(Media).filter_by(tmdb_id=tid, media_type=MediaType.TV).first()
        after[tid] = m2.status if m2 else None
    session.close()
    print(f"{'tmdb':>8} {'before':>7} {'after':>7}  result")
    print("-" * 50)
    for tid in TMDB_IDS:
        b, a = before[tid], after[tid]
        ok = (a in (MediaStatus.PARTIALLY_AVAILABLE, MediaStatus.AVAILABLE,
                     MediaStatus.OWNED_UNVERIFIED))
        print(f"{tid:>8} {str(b):>7} {str(a):>7}  {'OK 退出缺失' if ok else '!! 仍缺失'}")
    fixed = sum(1 for tid in TMDB_IDS if after[tid] in (4, 5))
    print(f"\n修复 {fixed}/{len(TMDB_IDS)} 部 (status ∈ {{4,5}} = 在库)")


if __name__ == "__main__":
    asyncio.run(main())
