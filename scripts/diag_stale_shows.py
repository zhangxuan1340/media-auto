#!/usr/bin/env python3
"""诊断: 为何 11 部剧在全量扫描后 media.status 仍为 1(UNKNOWN)/3(PROCESSING)
        尽管经 jellyfin_item 能关联到 jf_episode 实有集。

对比维度:
  media(tmdb_id, status, seasons)
  jellyfin_item(该 tmdb_id 的 Series 项, 其 item_id / tmdb_id)
  jf_episode(该 series 的实有集)
  → 看 episode-index 的 series→tmdb 映射是否能命中, 以及 media.seasons 是否为空。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import SessionLocal, init_db
from db.models import JellyfinItem, JfEpisode, Media, MediaType, Season

TMDB_IDS = [119017, 232766, 272104, 322512, 207148, 133547, 116498,
            115317, 202320, 15281, 596258]


def main():
    init_db()
    s = SessionLocal()
    print(f"{'tmdb':>8} {'media_st':>8} {'mj_type':>7} {'#seasons':>8} "
          f"{'ji_Series':>10} {'ji_tmdb':>9} {'eps':>5}  title / note")
    print("-" * 100)
    for tid in TMDB_IDS:
        media = s.query(Media).filter_by(tmdb_id=tid, media_type=MediaType.TV).first()
        mst = media.status if media else "ABSENT"
        mseasons = media.seasons if media else []
        # jellyfin_item 中该 tmdb_id 的 Series 项
        ji_series = s.query(JellyfinItem).filter_by(tmdb_id=str(tid), type="Series").all()
        ji_series = ji_series or s.query(JellyfinItem).filter_by(tmdb_id=tid, type="Series").all()
        ji_ids = [j.item_id for j in ji_series]
        # jf_episode 实有集(按 series_id)
        eps_counts = {}
        for jid in ji_ids:
            cnt = s.query(JfEpisode).filter_by(series_id=jid).count()
            if cnt:
                eps_counts[jid] = cnt
        # 总实有集
        total_eps = sum(eps_counts.values())
        note = ""
        if not ji_ids:
            note = "无 jellyfin_item Series 项(→ sids 空 → 找不到实有集)"
        elif not total_eps:
            note = "jellyfin_item 有 Series 但 jf_episode 无集"
        elif not mseasons:
            note = "media.seasons 为空(可能 TMDB all_seasons 返回空 → 未建季)"
        else:
            note = f"seasons 状态={[ (x.season_number, x.status) for x in mseasons]}"
        title = (media.title if media else "")
        print(f"{tid:>8} {str(mst):>8} {'TV':>7} {len(mseasons):>8} "
              f"{len(ji_ids):>10} {str(tid):>9} {total_eps:>5}  {title} | {note}")
        # 列出 ji 的 item_id 与对应 eps(便于核对 series→tmdb 命中)
        for jid in ji_ids:
            print(f"        ji.item_id={jid}  eps={eps_counts.get(jid,0)}")
    s.close()


if __name__ == "__main__":
    main()
