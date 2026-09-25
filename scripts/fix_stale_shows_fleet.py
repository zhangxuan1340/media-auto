#!/usr/bin/env python3
"""全量兜底: 把所有 media(media_type=TV, status∈{UNKNOWN=1, PROCESSING=3})
且 Jellyfin 实有 ≥1 集的剧, 用修复后的 _process_show 重推为 PARTIALLY_AVAILABLE。

这覆盖那 11 部之外、可能同样"有集却被判缺失"的剧(404 tmdb_id / Specials 异季
/ 季号错位等)。无实有集的(status 1/3 才是真未拥有/转码中)保持原样。
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


async def main():
    init_db()
    cfg = load_config()
    session = SessionLocal()
    series_to_tmdb, tmdb_to_series, eps_by_series = sc._load_episode_index(session)

    candidates = session.query(Media).filter(
        Media.media_type == MediaType.TV,
        Media.status.in_([MediaStatus.UNKNOWN, MediaStatus.PROCESSING]),
    ).all()
    print(f"候选 status∈{{1,3}} 的 TV 作品: {len(candidates)}")

    fixed = 0
    skipped = 0
    for m in candidates:
        sids = tmdb_to_series.get(str(m.tmdb_id), set())
        total_actual = sum(len(v) for sid in sids
                           for v in eps_by_series.get(sid, {}).values())
        if total_actual == 0:
            skipped += 1
            continue  # 真无实有集 → 保持原样(真未拥有/转码中)
        tseasons = await tmdb.all_seasons(cfg, m.tmdb_id) or []
        sc._process_show(session, m.tmdb_id, seasons=tseasons,
                         eps_by_series=eps_by_series, series_ids=sids)
        session.commit()
        fixed += 1
        print(f"  修复 tmdb={m.tmdb_id} ({m.title}) status→"
              f"{MediaStatus.PARTIALLY_AVAILABLE} 实有集={total_actual}")

    session.close()
    print(f"\n本次修复 {fixed} 部, 跳过(无实有集) {skipped} 部")


if __name__ == "__main__":
    asyncio.run(main())
