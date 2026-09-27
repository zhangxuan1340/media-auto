#!/usr/bin/env python3
"""media-auto / verify_numbering_guard —— 分集缺失「编号方案」回归
==============================================================================
背景(2026-09-27 用户反馈): 《火影忍者:疾风传》详情页显示
「已有 500 集 / 应有 500 集 · 缺 468 集 · 多 468 集」, Seerr 却是完整的。

根因: tmdb_season.episode_numbers 只在同步时存了 episode_count, **集号从没存过**
→ _season_expected_numbers 回退成 1..count 估算;而 TMDB 对长篇动画用
**绝对集号**(31910: S02=33..53 … S20=414..500), 本地 Jellyfin 同源亦然
→ 每季「实有 ∩ 应有 = ∅」→ 逐季缺N多N, 汇总正好 缺468 多468 (S01 恰好重合)。

本脚本锁死这套兜底(纯函数, 不碰库、不联网):
  ① _abs_season_ranges: 绝对集号区间按前序季集数累加(S0 不计入累计);
  ② 绝对集号 + 本地完整      → 不报缺失(缺0 多0);
  ③ 绝对集号 + 本地真缺 N 集 → 仍报缺 N(兜底不能掩盖真缺失);
  ④ 逐季编号(常见情况)      → 行为不变, 真缺仍报;
  ⑤ episode_numbers 已回填   → 权威集号说了算(兜底不介入);
  ⑥ S00 特别篇不计作品级缺失;
  ⑦ TMDB 加集后旧集号作废   → upsert_tmdb_seasons 集数变了就清 episode_numbers。

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


# 《火影忍者:疾风传》真实季结构(episodes = 每季集数)
SHIPPUDEN = [(0, 3), (1, 32), (2, 21), (3, 18), (4, 17), (5, 24), (6, 31),
             (7, 8), (8, 24), (9, 21), (10, 25), (11, 21), (12, 33),
             (13, 20), (14, 25), (15, 28), (16, 13), (17, 11), (18, 21),
             (19, 20), (20, 87)]


def _absolute_have():
    """本地 Jellyfin 侧(绝对集号)的实有集: {(season, episode)}。"""
    have, cum = set(), 0
    for sn, cnt in SHIPPUDEN:
        if sn == 0:
            have |= {(0, e) for e in range(1, cnt + 1)}
            continue
        have |= {(sn, e) for e in range(cum + 1, cum + cnt + 1)}
        cum += cnt
    return have


def _run(have, seasons=None, check_s0=False):
    s_map = {"s1": "31910"}
    ep_map = {"s1": have}
    seasons = seasons or [_Se(sn, cnt) for sn, cnt in SHIPPUDEN]
    return _series_missing(31910, seasons, s_map, ep_map, check_s0), seasons


def main():
    print("① _abs_season_ranges 绝对集号区间")
    ranges = _abs_season_ranges([_Se(sn, cnt) for sn, cnt in SHIPPUDEN])
    check("S01 = 1..32", ranges[1] == set(range(1, 33)))
    check("S02 = 33..53", ranges[2] == set(range(33, 54)), str(sorted(ranges[2]))[:60])
    check("S20 = 414..500", ranges[20] == set(range(414, 501)))
    check("S00 = 1..3(不计入累计)", ranges[0] == {1, 2, 3})
    check("估算 1..N 仍用于未回填季", _season_expected_numbers(_Se(2, 21)) == set(range(1, 22)))

    print("② 绝对集号 + 本地完整 → 不报缺失")
    (mc, xc, per, he, te), _ = _run(_absolute_have())
    check(f"缺0 多0 (got 缺{mc} 多{xc})", mc == 0 and xc == 0)
    check(f"500/500 (got {he}/{te})", he == 500 and te == 500)
    check("S01 完整", per[1]["missing"] == 0 and per[1]["extra"] == 0)
    check("S02 完整(绝对集号兜底生效)", per[2]["missing"] == 0 and per[2]["extra"] == 0,
          str(per[2]))

    print("③ 绝对集号 + 本地真缺 3 集 → 仍报缺 3")
    have = _absolute_have()
    have -= {(4, 75), (4, 76), (20, 455)}   # S04(72..88) 缺 2 集 + S20 缺 1 集
    (mc, xc, per, he, te), _ = _run(have)
    check(f"缺3 多0 (got 缺{mc} 多{xc})", mc == 3 and xc == 0)
    check("S04 缺 2", per[4]["missing"] == 2, str(per[4]))
    check("S20 缺 1", per[20]["missing"] == 1, str(per[20]))

    print("④ 逐季编号(常见情况)行为不变")
    seasons = [_Se(1, 10), _Se(2, 8)]
    have = {(1, e) for e in range(1, 11)} | {(2, e) for e in range(1, 8)}  # S01 全 / S02 缺 E08
    s_map = {"x": "31910"}
    mc, xc, per, he, te = _series_missing(31910, seasons, s_map, {"x": have}, False)
    check(f"缺1 多0 (got 缺{mc} 多{xc})", mc == 1 and xc == 0)
    check("S02 缺 E08", per[1]["missingEpisodes"] == [8], str(per[1]))

    print("⑤ episode_numbers 已回填 → 权威集号说了算")
    nums = list(range(33, 54))   # 权威: S02 = 33..53
    seasons = [_Se(2, 21, nums=json_list(nums))]
    have = {(2, e) for e in range(33, 53)}          # 缺最后一集 53
    s_map = {"x": "31910"}
    mc, xc, per, he, te = _series_missing(31910, seasons, s_map, {"x": have}, False)
    check(f"缺1 多0 (got 缺{mc} 多{xc})", mc == 1 and xc == 0)
    check("缺 E53", per[0]["missingEpisodes"] == [53], str(per[0]))
    check("权威集号不被绝对区间兜底覆盖", per[0]["extra"] == 0)

    print("⑥ S00 特别篇不计作品级缺失")
    full = _absolute_have()
    (mc, xc, per, he, te), _ = _run(full, check_s0=False)
    check("S00 行照常显示", per[0]["number"] == 0)
    check("S00 本地齐全 → 行内缺0", per[0]["missing"] == 0, str(per[0]))
    check("作品级仍为 0", mc == 0, f"got 缺{mc}")
    no_s0 = {p for p in full if p[0] != 0}
    (mc, xc, per, he, te), _ = _run(no_s0, check_s0=False)
    check("S00 缺3 行内可见", per[0]["missing"] == 3, str(per[0]))
    check("默认不计作品级(缺0)", mc == 0, f"got 缺{mc}")
    (mc1, *_), _ = _run(no_s0, check_s0=True)
    check("check_s0 打开后 S00 计入作品级(缺3)", mc1 == 3, f"got 缺{mc1}")

    print("⑦ TMDB 加集 → 旧集号作废(下次开详情重新回填)")
    init_db()
    s = SessionLocal()
    try:
        s.add(TmdbSeason(tmdb_id=31910, season_number=2, name="第 2 季",
                         episode_count=21, episode_numbers=json.dumps(list(range(33, 54)))))
        s.commit()
        # 集数没变 → 集号保留
        repo.upsert_tmdb_seasons(s, 31910, [{"number": 2, "name": "第 2 季",
                                             "episodes": 21, "air_date": "", "in_production": False}])
        s.commit()
        row = s.query(TmdbSeason).filter_by(tmdb_id=31910, season_number=2).one()
        check("集数未变 → 集号保留", bool(row.episode_numbers), row.episode_numbers[:40])
        # TMDB 加集(21 → 25) → 旧集号过期作废
        repo.upsert_tmdb_seasons(s, 31910, [{"number": 2, "name": "第 2 季",
                                             "episodes": 25, "air_date": "", "in_production": False}])
        s.commit()
        s.expire_all()
        row = s.query(TmdbSeason).filter_by(tmdb_id=31910, season_number=2).one()
        check("集数变了 → 集号清空", row.episode_numbers == "" and row.episode_count == 25,
              f"nums={row.episode_numbers[:20]!r} count={row.episode_count}")
        # 新季(无旧数据)正常写入
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


def json_list(nums):
    import json
    return json.dumps(nums)


if __name__ == "__main__":
    sys.exit(main())
