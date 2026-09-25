#!/usr/bin/env python3
"""复现 _load_episode_index + _process_item 的分集命中逻辑, 定位 sids / actual 为何空。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import SessionLocal, init_db
from db.models import JellyfinItem, JfEpisode, Media, MediaType, Season
import scripts.sync_jf_scanner as sc

TMDB_IDS = [119017, 232766, 272104, 322512, 207148, 133547, 116498,
            115317, 202320, 15281, 596258]


def main():
    init_db()
    s = SessionLocal()
    series_to_tmdb, tmdb_to_series, eps_by_series = sc._load_episode_index(s)
    print(f"index 规模: series_to_tmdb={len(series_to_tmdb)} "
          f"tmdb_to_series={len(tmdb_to_series)} "
          f"eps_by_series(series 数)={len(eps_by_series)}")
    print("-" * 90)
    for tid in TMDB_IDS:
        ji = s.query(JellyfinItem).filter_by(tmdb_id=str(tid), type="Series").first()
        ji = ji or s.query(JellyfinItem).filter_by(tmdb_id=tid, type="Series").first()
        ji_id = ji.item_id if ji else None
        sids = tmdb_to_series.get(str(tid), set())
        actual = set()
        for sid in sids:
            for ss, eps in eps_by_series.get(sid, {}).items():
                actual.add((ss, len(eps)))
        # jf_episode 直接按 ji_id 查(绕过索引)
        direct_eps = {}
        if ji_id:
            for sid, season, ep in s.query(JfEpisode.series_id, JfEpisode.season,
                                           JfEpisode.episode).filter_by(series_id=ji_id).all():
                direct_eps.setdefault(season, set()).add(ep)
        media = s.query(Media).filter_by(tmdb_id=tid, media_type=MediaType.TV).first()
        mseasons = [(x.season_number, x.status) for x in (media.seasons if media else [])]
        print(f"tmdb={tid}")
        print(f"   jellyfin_item.item_id = {ji_id}")
        print(f"   tmdb_to_series['{tid}'] = {sids}")
        print(f"   via_index actual(季,集数) = {sorted(actual)}")
        print(f"   direct jf_episode(季,集数) = {sorted((k, len(v)) for k, v in direct_eps.items())}")
        print(f"   media.seasons = {mseasons}")
    s.close()


if __name__ == "__main__":
    main()
