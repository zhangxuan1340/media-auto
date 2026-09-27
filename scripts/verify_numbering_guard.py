#!/usr/bin/env python3
"""media-auto / verify_numbering_guard —— 分集缺失「无真实集号不猜测」回归
==============================================================================
背景(2026-09-27 用户反馈): 《火影忍者:疾风传》详情页显示
「已有 500 集 / 应有 500 集 · 缺 468 集 · 多 468 集」, Seerr 却是完整的。

根因: tmdb_season.episode_numbers 只在同步时存了 episode_count, **集号从没存过** →
只能按 1..count 估算; 而 TMDB 对长篇动画用**绝对集号**(31910: S02=33..53 … S20=414..500),
本地 Jellyfin 同源亦然 → 每季「实有 ∩ 应有 = ∅」→ 逐季缺N多N, 汇总正好 缺468 多468。

用户定的口径: **没有 TMDB 真实集号就不猜**(猜错缺/多, 问题就大了)。本脚本锁死:
  ① _abs_season_ranges: 绝对集号区间按前序季集数累加(S0 不计入累计);
  ② 绝对集号 + 本地完整(未回填): 与绝对区间**完全一致** → 缺0 多0, numbersSynced=True;
  ③ 未回填 + 无法确证(真缺 / 逐季编号局部缺) → numbersSynced=False、missingCount=None,
     **一个缺/多都不报**; 回填真实集号后 → 逐集精确(缺3 / 缺1);
  ④ episode_numbers 已回填 → 权威集号说了算(逐集清单也给);
  ⑤ 本地该季一集都没有 → 缺全季(集数为准, 任何编号方案都成立) → 缺失页"未拥有"照常;
  ⑥ S00 特别篇不计作品级缺失(默认); check_s0 打开才计;
  ⑦ TMDB 加集 → 旧集号作废(下次开详情重新回填)。

用法: ./venv/bin/python scripts/verify_numbering_guard.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* / server.* 之前指好库路径(db/database.py 导入时就建 engine)
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_numbering_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import SessionLocal, init_db          # noqa: E402
from db import repositories as repo                    # noqa: E402
from db.models import TmdbSeason                       # noqa: E402
from server.routers.browse import (                    # noqa: E402
    _abs_season_ranges, _season_expected_numbers, _series_missing,
)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


class _Se:
    """tmdb_season 行的最小替身(_series_missing/_abs_season_ranges 只读这些属性)。"""

    def __init__(self, number, count, nums="", name=""):
        self.season_number = number
        self.name = name
        self.episode_count = count
        self.episode_numbers = nums
        self.in_production = False


# 《火影忍者:疾风传》真实季结构(季号, 集数)
SHIPPUDEN = [(0, 3), (1, 32), (2, 21), (3, 18), (4, 17), (5, 24), (6, 31),
             (7, 8), (8, 24), (9, 21), (10, 25), (11, 21), (12, 33),
             (13, 20), (14, 25), (15, 28), (16, 13), (17, 11), (18, 21),
             (19, 20), (20, 87)]


def _seasons(hydrated=False):
    """季行。hydrated=True → 带 TMDB 真实集号(绝对集号); 否则 episode_numbers 全空。"""
    out, cum = [], 0
    for sn, cnt in SHIPPUDEN:
        nums = None
        if hydrated:
            nums = list(range(1, cnt + 1)) if sn == 0 else list(range(cum + 1, cum + cnt + 1))
        out.append(_Se(sn, cnt, nums=json.dumps(nums) if nums else ""))
        if sn:
            cum += cnt
    return out


def _absolute_have(with_s0=True):
    """本地 Jellyfin 侧(绝对集号)的实有集: {(season, episode)}。"""
    have, cum = set(), 0
    for sn, cnt in SHIPPUDEN:
        if sn == 0:
            if with_s0:
                have |= {(0, e) for e in range(1, cnt + 1)}
            continue
        have |= {(sn, e) for e in range(cum + 1, cum + cnt + 1)}
        cum += cnt
    return have


def _run(have, seasons=None, check_s0=False, tmdb_id=31910):
    s_map = {"s1": str(tmdb_id)}
    ep_map = {"s1": have}
    seasons = seasons if seasons is not None else _seasons()
    return _series_missing(tmdb_id, seasons, s_map, ep_map, check_s0)


def main():
    print("① _abs_season_ranges 绝对集号区间")
    ranges = _abs_season_ranges(_seasons())
    check("S01 = 1..32", ranges[1] == set(range(1, 33)))
    check("S02 = 33..53", ranges[2] == set(range(33, 54)), str(sorted(ranges[2]))[:60])
    check("S20 = 414..500", ranges[20] == set(range(414, 501)))
    check("S00 = 1..3(不计入累计)", ranges[0] == {1, 2, 3})
    check("估算 1..N 只用于「应有集数」展示", _season_expected_numbers(_Se(2, 21)) == set(range(1, 22)))

    print("② 绝对集号 + 本地完整(未回填) → 与绝对区间完全一致 → 缺0 多0")
    mc, xc, per, he, te, nsync = _run(_absolute_have())
    check(f"缺0 多0 (got 缺{mc} 多{xc})", mc == 0 and xc == 0)
    check(f"numbersSynced=True (got {nsync})", nsync is True)
    check(f"500/500 (got {he}/{te})", he == 500 and te == 500)
    check("S01 numbersKnown", per[1]["numbersKnown"] is True, str(per[1]))
    check("S02 numbersKnown(绝对区间完全一致)", per[2]["numbersKnown"] is True, str(per[2]))

    print("③ 未回填 + 无法确证 → 不报缺/多; 回填后精确报缺3")
    have = _absolute_have() - {(4, 75), (4, 76), (20, 455)}   # 真缺 3 集
    mc, xc, per, he, te, nsync = _run(have)
    check(f"missingCount=None (got {mc})", mc is None)
    check(f"extraCount=0 (got {xc})", xc == 0)
    check(f"numbersSynced=False (got {nsync})", nsync is False)
    check("S04 行内不报缺", per[4]["missing"] == 0 and per[4]["numbersKnown"] is False, str(per[4]))
    check("S04 不给猜测的集号清单", per[4]["missingEpisodes"] == [], str(per[4]))
    mc, xc, per, he, te, nsync = _run(have, seasons=_seasons(hydrated=True))
    check(f"回填后 缺3 多0 (got 缺{mc} 多{xc})", mc == 3 and xc == 0)
    check("回填后 numbersSynced=True", nsync is True)
    check("S04 缺 75/76", per[4]["missingEpisodes"] == [75, 76], str(per[4]))
    check("S20 缺 455", per[20]["missingEpisodes"] == [455], str(per[20]))

    print("④ 逐季编号 + 局部缺(未回填) → 不报缺/多; 回填后精确")
    seasons = [_Se(1, 10), _Se(2, 8)]
    have = {(1, e) for e in range(1, 11)} | {(2, e) for e in range(1, 8)}  # S01 全 / S02 缺 E08
    mc, xc, per, he, te, nsync = _series_missing(31910, seasons, {"x": "31910"}, {"x": have}, False)
    check(f"未回填 → missingCount=None (got {mc})", mc is None)
    check(f"未回填 → numbersSynced=False (got {nsync})", nsync is False)
    check("S01 与绝对区间1..10 一致 → 可确证 缺0", per[0]["numbersKnown"] is True
          and per[0]["missing"] == 0, str(per[0]))
    check("S02 局部缺 → 不猜(numbersKnown=False)", per[1]["numbersKnown"] is False
          and per[1]["missing"] == 0, str(per[1]))
    seasons = [_Se(1, 10, nums=json.dumps(list(range(1, 11)))),
               _Se(2, 8, nums=json.dumps(list(range(1, 9))))]
    mc, xc, per, he, te, nsync = _series_missing(31910, seasons, {"x": "31910"}, {"x": have}, False)
    check(f"回填后 缺1 多0 (got 缺{mc} 多{xc})", mc == 1 and xc == 0)
    check("缺 E08", per[1]["missingEpisodes"] == [8], str(per[1]))

    print("⑤ 本地该季无集 → 缺全季(集数为准, 与编号方案无关)")
    mc, xc, per, he, te, nsync = _run(set())
    check(f"未在库 → 缺500 (got {mc})", mc == 500 and xc == 0)
    check(f"numbersSynced=True (got {nsync})", nsync is True)
    check("S02 缺21(按集数)", per[2]["missing"] == 21 and per[2]["numbersKnown"] is True,
          str(per[2]))

    print("⑥ episode_numbers 已回填 → 权威集号说了算")
    seasons = [_Se(2, 21, nums=json.dumps(list(range(33, 54))))]   # 权威: S02 = 33..53
    have = {(2, e) for e in range(33, 53)}                          # 缺最后一集 53
    mc, xc, per, he, te, nsync = _series_missing(31910, seasons, {"x": "31910"}, {"x": have}, False)
    check(f"缺1 多0 (got 缺{mc} 多{xc})", mc == 1 and xc == 0)
    check("缺 E53", per[0]["missingEpisodes"] == [53], str(per[0]))
    check("权威集号不被绝对区间兜底覆盖", per[0]["extra"] == 0 and per[0]["numbersKnown"] is True)

    print("⑦ S00 特别篇不计作品级缺失")
    mc, xc, per, he, te, nsync = _run(_absolute_have(), check_s0=False)
    check("S00 行照常显示", per[0]["number"] == 0)
    check("S00 本地齐全 → 行内缺0", per[0]["missing"] == 0, str(per[0]))
    check("作品级仍为 0", mc == 0, f"got 缺{mc}")
    no_s0 = _absolute_have(with_s0=False)
    mc, xc, per, he, te, nsync = _run(no_s0, check_s0=False)
    check("S00 本地无集 → 缺3(行内)", per[0]["missing"] == 3, str(per[0]))
    check("默认不计作品级(缺0)", mc == 0, f"got 缺{mc}")
    mc1, *_ = _run(no_s0, check_s0=True)
    check("check_s0 打开后 S00 计入作品级(缺3)", mc1 == 3, f"got 缺{mc1}")

    print("⑧ TMDB 加集 → 旧集号作废(下次开详情重新回填)")
    init_db()
    s = SessionLocal()
    try:
        s.add(TmdbSeason(tmdb_id=31910, season_number=2, name="第 2 季",
                         episode_count=21, episode_numbers=json.dumps(list(range(33, 54)))))
        s.commit()
        repo.upsert_tmdb_seasons(s, 31910, [{"number": 2, "name": "第 2 季",
                                             "episodes": 21, "air_date": "", "in_production": False}])
        s.commit()
        row = s.query(TmdbSeason).filter_by(tmdb_id=31910, season_number=2).one()
        check("集数未变 → 集号保留", bool(row.episode_numbers), row.episode_numbers[:40])
        repo.upsert_tmdb_seasons(s, 31910, [{"number": 2, "name": "第 2 季",
                                             "episodes": 25, "air_date": "", "in_production": False}])
        s.commit()
        s.expire_all()
        row = s.query(TmdbSeason).filter_by(tmdb_id=31910, season_number=2).one()
        check("集数变了 → 集号清空", row.episode_numbers == "" and row.episode_count == 25,
              f"nums={row.episode_numbers[:20]!r} count={row.episode_count}")
        repo.upsert_tmdb_seasons(s, 31910, [{"number": 21, "name": "新季",
                                             "episodes": 0, "air_date": "", "in_production": False}])
        s.commit()
        check("新季插入不报错", s.query(TmdbSeason).filter_by(tmdb_id=31910, season_number=21).count() == 1)
    finally:
        s.close()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ numbering guard: 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
