#!/usr/bin/env python3
"""用新所有权兜底(区分"不完整"4 与"库内·完整性未知"8)重分类所有 TV 作品。

逻辑: 对 status∈{4(PARTIALLY), 8(OWNED_UNVERIFIED)} 的 TV 行, 重新拉 TMDB 季结构:
  - 实有集 >0 且 TMDB 有季(tseasons 非空) → 4(明确不完整)
  - 实有集 >0 且 TMDB 无季(404/挂错 ID)   → 8(库内·完整性未知)
实有集 = 0 的保持原样(不该是 4/8)。
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
    rows = session.query(Media).filter(
        Media.media_type == MediaType.TV,
        Media.status.in_([MediaStatus.PARTIALLY_AVAILABLE,
                          MediaStatus.OWNED_UNVERIFIED])).all()
    print(f"待重分类 TV 行(status∈{{4,8}}): {len(rows)}")
    to4, to8, keep = 0, 0, 0
    for m in rows:
        sids = tmdb_to_series.get(str(m.tmdb_id), set())
        total_actual = sum(len(v) for sid in sids
                           for v in eps_by_series.get(sid, {}).values())
        if total_actual == 0:
            keep += 1
            continue
        tseasons = await tmdb.all_seasons(cfg, m.tmdb_id) or []
        if tseasons:
            if m.status != MediaStatus.PARTIALLY_AVAILABLE:
                m.status = MediaStatus.PARTIALLY_AVAILABLE
                to4 += 1
                print(f"  {m.tmdb_id} ({m.title}) → 4 不完整")
        else:
            if m.status != MediaStatus.OWNED_UNVERIFIED:
                m.status = MediaStatus.OWNED_UNVERIFIED
                to8 += 1
                print(f"  {m.tmdb_id} ({m.title}) → 8 库内·完整性未知 (TMDB 404/无季)")
        session.commit()  # 逐行提交, 慢 TMDB 调用也不丢进度
    session.close()
    print(f"\n→ 4 不完整: {to4}, 8 库内·未确认: {to8}, 保持: {keep}")


if __name__ == "__main__":
    asyncio.run(main())
