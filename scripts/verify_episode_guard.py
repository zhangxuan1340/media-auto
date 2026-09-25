#!/usr/bin/env python3
"""media-auto / verify_episode_guard —— 分集写入模型回归
==============================================================================
2026-09-22 定稿后, 分集(jf_episode)的写入模型是:

  · **按剧替换**: 只替换"这份快照覆盖到的剧", 未覆盖的剧原样保留;
  · **永不清空整表**: 刻意删除了 `wipe_jf_episodes` 这类接口;
  · **不做"快照完整性"判断**: 一份残缺列表的最坏后果是"某部剧少显示几集",
    下一轮窗口增量 / 明日全量会重新覆盖它 —— 用闸门防"少写几条"不划算,
    真正要防的"删掉完整数据"已由写入模型从根上消除(用户 2026-09-22 明确要求)。

本脚本用**临时库 + 假 Jellyfin**验证这套模型(不碰真实数据):
  ① 增量补分集(_refresh_episodes_for_series): 单剧失败只跳过该剧, 旧数据保留;
  ② 全量刷新(_run_async full): 未覆盖到的剧一行不动(不会"一次踩少全部");
  ③ 窗口增量(_run_async recent): 只碰窗口里的剧, 窗口外的剧不被刷新。

用法: ./venv/bin/python scripts/verify_episode_guard.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* 之前指好库路径: db/database.py 在导入时就建 engine
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_epmodel_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import SessionLocal, init_db          # noqa: E402
from db import models as M                             # noqa: E402
from clients.jellyfin import client as jf              # noqa: E402
from scripts import sync_jf_scanner as sc              # noqa: E402

CFG = {}


def _item(i, name, kind="Series", tmdb="100"):
    return {"item_id": i, "library_id": "lib1", "name": name, "type": kind,
            "year": 2020, "path": f"/Cloud/{name}", "tmdb_id": tmdb, "imdb_id": "",
            "overview": "", "date_created": jf.parse_jf_time("2026-09-22T10:00:00.0000000Z")}


def _ep(sid, se, ep, name=""):
    return {"series_id": sid, "season": se, "episode": ep, "name": name}


class FakeJF:
    """假 Jellyfin: 全量分集 / 单剧分集 / 窗口接口 / 全库条目 都可注入。"""

    def __init__(self, items=None, eps=None, eps_by_series=None, probe_raises=(),
                 recent_items=None, recent_ep_series=None):
        self.items = items or []
        self.eps = eps or []
        self.eps_by_series = eps_by_series or {}
        self.probe_raises = set(probe_raises)
        self.recent_items = recent_items or []
        self.recent_ep_series = recent_ep_series or []

    async def list_items(self, cfg, library_id=None, limit=None, types="Movie,Series"):
        return self.items

    async def list_episodes(self, cfg, library_id=None):
        return self.eps

    async def list_episodes_for_series(self, cfg, series_id):
        if series_id in self.probe_raises:
            raise RuntimeError("probe failed(模拟 404/取不到)")
        return self.eps_by_series.get(series_id, [])

    async def list_recent_items(self, cfg, limit=None, types="Movie,Series"):
        return self.recent_items

    async def list_recent_episode_series(self, cfg, limit=None):
        return self.recent_ep_series


def _patch(fake):
    jf.list_items = fake.list_items
    jf.list_episodes = fake.list_episodes
    jf.list_episodes_for_series = fake.list_episodes_for_series
    jf.list_recent_items = fake.list_recent_items
    jf.list_recent_episode_series = fake.list_recent_episode_series


def _seed_episodes(rows):
    s = SessionLocal()
    s.query(M.JfEpisode).delete()
    for sid, se, ep, name in rows:
        s.add(M.JfEpisode(series_id=sid, season=se, episode=ep, name=name))
    s.commit()
    s.close()


def _seed_media(rows):
    """rows = [(tmdb_id, status, jellyfin_media_id)] —— 全为剧集。"""
    s = SessionLocal()
    s.query(M.Season).delete()
    s.query(M.Media).delete()
    for tmdb_id, status, jfid in rows:
        s.add(M.Media(tmdb_id=tmdb_id, media_type=M.MediaType.TV, status=status,
                      title=f"t{tmdb_id}", jellyfin_media_id=jfid))
    s.commit()
    s.close()


def _ep_counts():
    s = SessionLocal()
    try:
        out = {}
        for sid, _s, _e in s.query(M.JfEpisode.series_id, M.JfEpisode.season,
                                   M.JfEpisode.episode).all():
            out[sid] = out.get(sid, 0) + 1
        return out
    finally:
        s.close()


def _report(title, ok, detail):
    print(f"  [{'PASS' if ok else 'FAIL'}] {title}: {detail}")
    return ok


def _case_incr(title, *, local_eps, eps_by_series, probe_raises, series_ids, expect_ok_fail,
               expect_counts):
    _patch(FakeJF(eps_by_series=eps_by_series, probe_raises=probe_raises))
    _seed_episodes(local_eps)
    session = SessionLocal()
    try:
        ok, fail = asyncio.run(
            sc._refresh_episodes_for_series(CFG, session, series_ids))
    finally:
        session.close()
    got = _ep_counts()
    passed = ((ok, fail) == expect_ok_fail
              and all(got.get(k, 0) == v for k, v in expect_counts.items()))
    return _report(title, passed,
                   f"(成功,失败)=({ok}, {fail})(期望 {expect_ok_fail}), "
                   f"分集 {got}(期望 {expect_counts})")


def _case_run(title, *, mode, local_eps, full_eps=None, recent_ep_series=None,
              eps_by_series=None, expect_counts):
    _patch(FakeJF(items=[], eps=full_eps or [], recent_items=[],
                  recent_ep_series=recent_ep_series or [],
                  eps_by_series=eps_by_series or {}))
    _seed_episodes(local_eps)
    _seed_media([("100", M.MediaStatus.AVAILABLE, "s1")])
    asyncio.run(sc._run_async(CFG, mode=mode))
    got = _ep_counts()
    passed = all(got.get(k, 0) == v for k, v in expect_counts.items())
    return _report(title, passed, f"分集 {got}(期望 {expect_counts})")


def main():
    init_db()
    results = []
    print("分集写入模型回归(临时库: %s)" % os.environ["MEDIA_AUTO_DB"])

    print("\n① 增量补分集: 单剧失败只跳过该剧, 旧数据保留")
    results.append(_case_incr(
        "s2 拉取抛错 → 跳过 s2(旧 5 集保留), s1 正常替换为 3 集",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 9)]
                  + [("s2", 1, i, f"E{i}") for i in range(1, 6)],
        eps_by_series={"s1": [_ep("s1", 1, i) for i in range(1, 4)]},
        probe_raises=("s2",), series_ids=["s1", "s2"],
        expect_ok_fail=(1, 1), expect_counts={"s1": 3, "s2": 5}))
    results.append(_case_incr(
        "快照比本地少(旧 8 集 / 快照 3 集) → 按快照替换(已知取舍: 下一轮会刷新回来)",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 9)],
        eps_by_series={"s1": [_ep("s1", 1, i) for i in range(1, 4)]},
        probe_raises=(), series_ids=["s1"],
        expect_ok_fail=(1, 0), expect_counts={"s1": 3}))
    results.append(_case_incr(
        "快照为空(该剧分集全没了) → 该剧清空, 其它剧不受影响",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 6)]
                  + [("s2", 1, i, f"E{i}") for i in range(1, 4)],
        eps_by_series={"s1": []},
        probe_raises=(), series_ids=["s1"],
        expect_ok_fail=(1, 0), expect_counts={"s1": 0, "s2": 3}))

    print("\n② 全量刷新(_run_async full): 未覆盖到的剧一行不动")
    results.append(_case_run(
        "全量分集快照只覆盖 s1 → s1 替换为 3 集, s2 的 5 集保留",
        mode="full",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 9)]
                  + [("s2", 1, i, f"E{i}") for i in range(1, 6)],
        full_eps=[_ep("s1", 1, i) for i in range(1, 4)],
        expect_counts={"s1": 3, "s2": 5}))
    results.append(_case_run(
        "全量分集快照为空 → 所有剧一行不少(旧写法会把整表清空)",
        mode="full",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 9)]
                  + [("s2", 1, i, f"E{i}") for i in range(1, 6)],
        full_eps=[],
        expect_counts={"s1": 8, "s2": 5}))

    print("\n③ 窗口增量(_run_async recent): 只碰窗口里的剧")
    results.append(_case_run(
        "窗口只含 s1 → s1 替换为 4 集, 窗口外的 s2 原样保留",
        mode="recent",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 3)]
                  + [("s2", 1, i, f"E{i}") for i in range(1, 6)],
        recent_ep_series=["s1"],
        eps_by_series={"s1": [_ep("s1", 1, i) for i in range(1, 5)]},
        expect_counts={"s1": 4, "s2": 5}))
    results.append(_case_run(
        "窗口为空 → 什么都不刷新(不依赖任何游标状态)",
        mode="recent",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 3)],
        recent_ep_series=[],
        expect_counts={"s1": 2}))

    total, ok = len(results), sum(1 for r in results if r)
    print(f"\n结果: {ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
