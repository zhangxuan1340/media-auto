#!/usr/bin/env python3
"""media-auto / verify_sync_guard —— 「纯 upsert + fail-safe 删除」模型回归
==============================================================================
背景(2026-09-22 照 Seerr 重做)
------------------------------
旧模型的失效模式: 同步器**先清空本地再按远端列表重建**, 或用**远端列表的形状**
去 diff "陈旧行"。Jellyfin 正在扫库时返回的是**残缺列表**(实测《鬼语者》107 集
只回 24 集), 于是一份残缺快照就把完整数据洗成缺失数据。

新模型(与 Seerr 一致):
  · **写入永远可加**: 同步只 upsert / 按剧替换, **从不删除任何行**;
  · **删除只由 availability_sync 做**, 且 **fail-safe**: 按 ID 单查, **只有明确查不到**
    才算消失; 请求失败/未知一律当作"还在"。

本脚本用**临时库 + 假 Jellyfin**验证这两条不变量(不碰真实数据):
  ① A 层(jellyfin_item)  —— 残缺快照/空快照 都不能让本地少一行;
  ② 分集(jf_episode)     —— 全量刷新只替换覆盖到的剧, 未覆盖的剧原样保留;
  ③ 出库对账            —— 未知→保留, 明确查不到→才删;
  ④ 季级对账            —— 分集列表取不到→不动季状态; 明确某季没了→才删+降级。

用法: ./venv/bin/python scripts/verify_sync_guard.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* 之前指好库路径: db/database.py 在导入时就建 engine
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_model_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import SessionLocal, init_db          # noqa: E402
from db import models as M                             # noqa: E402
from clients.jellyfin import client as jf              # noqa: E402
from scripts import sync_jellyfin as sj                # noqa: E402
from scripts import sync_jf_scanner as sc              # noqa: E402
from scripts import availability_sync as av            # noqa: E402

CFG = {}


def _item(i, name, kind="Series", tmdb="100"):
    """造一条**已归一化**的 Jellyfin 条目(与 _norm_items 输出同形)。"""
    return {"item_id": i, "library_id": "lib1", "name": name, "type": kind,
            "year": 2020, "path": f"/Cloud/{name}", "tmdb_id": tmdb, "imdb_id": "",
            "overview": "", "date_created": jf.parse_jf_time("2026-09-22T10:00:00.0000000Z")}


def _ep(sid, se, ep, name=""):
    """造一条 list_episodes / list_episodes_for_series 输出形状的分集。"""
    return {"series_id": sid, "season": se, "episode": ep, "name": name}


class FakeJF:
    """假 Jellyfin: 全库条目 / 全量分集 / 单剧分集 / 按 ID 单查 都可注入。

    exists: {item_id: True|False|None} —— 直接模拟 item_exists 的**三态**返回;
            缺省的 id 用 `default_exists`。
    probe_raises: list_episodes_for_series 抛异常的剧集合(模拟"取不到"→不可判断)。
    """

    def __init__(self, items=None, eps=None, eps_by_series=None, exists=None,
                 default_exists=True, probe_raises=()):
        self.items = items or []
        self.eps = eps or []
        self.eps_by_series = eps_by_series or {}
        self.exists = exists or {}
        self.default_exists = default_exists
        self.probe_raises = set(probe_raises)

    async def list_items(self, cfg, library_id=None, limit=None, types="Movie,Series"):
        return self.items

    async def list_episodes(self, cfg, library_id=None):
        return self.eps

    async def list_episodes_for_series(self, cfg, series_id):
        if series_id in self.probe_raises:
            raise RuntimeError("probe failed(模拟取不到)")
        return self.eps_by_series.get(series_id, [])

    async def item_exists(self, cfg, item_id):
        if str(item_id) in self.exists:
            return self.exists[str(item_id)]
        return self.default_exists


def _patch(fake):
    """把假客户端接到被验证的代码路径上(模块级名字, 调用时解析)。"""
    jf.list_items = fake.list_items
    jf.list_episodes = fake.list_episodes
    jf.list_episodes_for_series = fake.list_episodes_for_series
    jf.item_exists = fake.item_exists


def _reset_items(rows):
    """重置 A 层条目表。rows = (item_id, name) 或 (item_id, name, tmdb_id)。

    ⚠️ tmdb_id 要和 media 行对得上: availability_sync 有一条"残留错行"判定 ——
    A 层里该 JF item 的 tmdb 与 media 行的 tmdb 不一致时, 这行会被判为旧错 ID 而
    标成 DELETED(测试数据造错会得到假结论)。
    """
    s = SessionLocal()
    s.query(M.JellyfinItem).delete()
    for row in rows:
        iid, name = row[0], row[1]
        tmdb = row[2] if len(row) > 2 else "100"
        s.add(M.JellyfinItem(item_id=iid, library_id="lib1", name=name, type="Series",
                             year=2020, path=f"/Cloud/{name}", tmdb_id=tmdb))
    s.commit()
    s.close()


def _item_ids():
    s = SessionLocal()
    try:
        return {r.item_id for r in s.query(M.JellyfinItem).all()}
    finally:
        s.close()


def _seed_episodes(rows):
    """重置分集表: rows = [(series_id, season, episode, name)]"""
    s = SessionLocal()
    s.query(M.JfEpisode).delete()
    for sid, se, ep, name in rows:
        s.add(M.JfEpisode(series_id=sid, season=se, episode=ep, name=name))
    s.commit()
    s.close()


def _ep_counts():
    """{series_id: 行数}"""
    s = SessionLocal()
    try:
        out = {}
        for sid, _s, _e in s.query(M.JfEpisode.series_id, M.JfEpisode.season,
                                   M.JfEpisode.episode).all():
            out[sid] = out.get(sid, 0) + 1
        return out
    finally:
        s.close()


def _seed_media(rows):
    """重置 media/season 表: rows = [(tmdb_id, media_type, status, jellyfin_media_id, seasons)]

    seasons: [(季号, 状态)] —— 仅剧集需要, 可为 None。
    """
    s = SessionLocal()
    s.query(M.Season).delete()
    s.query(M.Media).delete()
    s.commit()
    for tmdb_id, mtype, status, jfid, seasons in rows:
        m = M.Media(tmdb_id=tmdb_id, media_type=mtype, status=status,
                    title=f"t{tmdb_id}", jellyfin_media_id=jfid)
        s.add(m)
        s.flush()
        for num, st in (seasons or []):
            s.add(M.Season(media_id=m.id, season_number=num, status=st))
    s.commit()
    s.close()


def _media_states():
    """{tmdb_id: (作品状态, {季号: 季状态})}"""
    s = SessionLocal()
    try:
        out = {}
        for m in s.query(M.Media).options().all():
            out[m.tmdb_id] = (m.status, {x.season_number: x.status for x in m.seasons})
        return out
    finally:
        s.close()


def _report(title, ok, detail):
    print(f"  [{'PASS' if ok else 'FAIL'}] {title}: {detail}")
    return ok


# ---------------------------------------------------------------------------
# ① A 层: 残缺/空快照都不能让本地少一行
# ---------------------------------------------------------------------------
def _case_items(title, *, local_rows, snap_items, full, expect_rows):
    _patch(FakeJF(items=snap_items))
    _reset_items(local_rows)
    session = SessionLocal()
    try:
        asyncio.run(sj._sync_items(CFG, session, full=full))
    finally:
        session.close()
    after = _item_ids()
    return _report(title, len(after) == expect_rows,
                   f"本地条目 {len(after)} 条(期望 {expect_rows})")


# ---------------------------------------------------------------------------
# ② 分集: 全量刷新只替换覆盖到的剧
# ---------------------------------------------------------------------------
def _case_episodes(title, *, local_eps, snap_eps, expect):
    """expect: {series_id: 行数} —— 未出现的 series 视为"应为 0"(不该存在)。"""
    _patch(FakeJF(eps=snap_eps))
    _seed_episodes(local_eps)
    session = SessionLocal()
    try:
        asyncio.run(sc._refresh_all_episodes(CFG, session))
    finally:
        session.close()
    got = _ep_counts()
    ok = all(got.get(k, 0) == v for k, v in expect.items())
    return _report(title, ok, f"分集分布 {got}(期望 {expect})")


# ---------------------------------------------------------------------------
# ③④ 出库对账: fail-safe
# ---------------------------------------------------------------------------
def _case_avail(title, *, media_rows, a_rows, live_items, exists=None,
                default_exists=True, probe_raises=(), eps_by_series=None,
                expect):
    _patch(FakeJF(items=live_items, exists=exists, default_exists=default_exists,
                  probe_raises=probe_raises, eps_by_series=eps_by_series))
    _reset_items(a_rows)
    _seed_media(media_rows)
    stats = asyncio.run(av._run_async(CFG))
    got = _media_states()
    ok = got == expect
    return _report(title, ok, f"状态 {got}(期望 {expect})  stats={stats}")


def main():
    init_db()
    results = []
    AV, PA = M.MediaStatus.AVAILABLE, M.MediaStatus.PARTIALLY_AVAILABLE
    DEL = M.MediaStatus.DELETED
    MV, TV = M.MediaType.MOVIE, M.MediaType.TV
    print("同步写入模型回归(临时库: %s)" % os.environ["MEDIA_AUTO_DB"])

    print("\n① A 层(jellyfin_item): 残缺快照不得让本地少一行")
    results.append(_case_items(
        "全量: 快照残缺(本地 5 / 快照 2) → 5 行全在",
        local_rows=[("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"), ("e", "E")],
        snap_items=[_item("a", "A"), _item("b", "B")],
        full=True, expect_rows=5))
    results.append(_case_items(
        "全量: 快照为空 → 5 行全在(旧写法会清空整表)",
        local_rows=[("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"), ("e", "E")],
        snap_items=[], full=True, expect_rows=5))
    results.append(_case_items(
        "全库比对: 快照残缺(本地 3 / 快照 2) → 3 行全在(不再清陈旧行)",
        local_rows=[("a", "A"), ("b", "B"), ("c", "C")],
        snap_items=[_item("a", "A"), _item("b", "B")],
        full=False, expect_rows=3))
    results.append(_case_items(
        "全量: 完整快照(新增 4) → 新增 4 行, 本地原有 2 行不删(共 6)",
        local_rows=[("a", "A"), ("b", "B")],
        snap_items=[_item(str(i), f"N{i}") for i in range(4)],
        full=True, expect_rows=6))

    print("\n② 分集(jf_episode): 只替换覆盖到的剧, 未覆盖的原样保留")
    results.append(_case_episodes(
        "快照只覆盖 s1 → s1 被替换(3 集), s2 的 5 集原样保留",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 9)]
                  + [("s2", 1, i, f"E{i}") for i in range(1, 6)],
        snap_eps=[_ep("s1", 1, i) for i in range(1, 4)],
        expect={"s1": 3, "s2": 5}))
    results.append(_case_episodes(
        "快照为空 → 所有剧的分集一行不少(旧写法会把整表清空)",
        local_eps=[("s1", 1, i, f"E{i}") for i in range(1, 9)]
                  + [("s2", 1, i, f"E{i}") for i in range(1, 6)],
        snap_eps=[],
        expect={"s1": 8, "s2": 5}))

    print("\n③ 出库对账: 只有「明确查不到」才删, 未知一律保留")
    results.append(_case_avail(
        "单查返回未知(None) → 作品保留(AVAILABLE)",
        media_rows=[(1, MV, AV, "jf1", None)], a_rows=[("jf1", "JF1", "1")],
        live_items=[], exists={"jf1": None},
        expect={1: (AV, {})}))
    results.append(_case_avail(
        "单查明确查不到(False) → 作品标 DELETED",
        media_rows=[(1, MV, AV, "jf1", None)], a_rows=[("jf1", "JF1", "1")],
        live_items=[], exists={"jf1": False},
        expect={1: (DEL, {})}))
    results.append(_case_avail(
        "单查明确还在(True) → 作品保留",
        media_rows=[(1, MV, AV, "jf1", None)], a_rows=[("jf1", "JF1", "1")],
        live_items=[], exists={"jf1": True},
        expect={1: (AV, {})}))
    results.append(_case_avail(
        "快路命中(在库列表里有) → 作品保留, 不发单查",
        media_rows=[(1, MV, AV, "jf1", None)], a_rows=[("jf1", "JF1", "1")],
        live_items=[_item("jf1", "JF1", "Movie")], exists={"jf1": False},
        expect={1: (AV, {})}))

    print("\n④ 季级对账: 取不到就不动, 明确没了才删+降级")
    results.append(_case_avail(
        "分集列表取不到(抛错) → 季状态一动不动",
        media_rows=[(1, TV, AV, "jfT", [(1, AV), (2, AV)])],
        a_rows=[("jfT", "JFT", "1")],
        live_items=[_item("jfT", "JFT", "Series")],
        probe_raises=("jfT",),
        expect={1: (AV, {1: AV, 2: AV})}))
    results.append(_case_avail(
        "明确只剩 S1 → S2 标 DELETED, 作品降为 PARTIALLY_AVAILABLE",
        media_rows=[(1, TV, AV, "jfT", [(1, AV), (2, AV)])],
        a_rows=[("jfT", "JFT", "1")],
        live_items=[_item("jfT", "JFT", "Series")],
        eps_by_series={"jfT": [_ep("jfT", 1, i) for i in range(1, 4)]},
        expect={1: (PA, {1: AV, 2: DEL})}))
    results.append(_case_avail(
        "分集列表为空(某季真的清空了) → 该季 DELETED",
        media_rows=[(1, TV, AV, "jfT", [(1, AV), (2, AV)])],
        a_rows=[("jfT", "JFT", "1")],
        live_items=[_item("jfT", "JFT", "Series")],
        eps_by_series={"jfT": []},
        expect={1: (PA, {1: DEL, 2: DEL})}))

    total, ok = len(results), sum(1 for r in results if r)
    print(f"\n结果: {ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
