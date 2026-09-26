#!/usr/bin/env python3
"""
media-auto / availability_sync —— 可用性对账(适配本机 Jellyfin)
==============================================================================
  - 逐条检查"本地标记为 AVAILABLE / PARTIALLY_AVAILABLE 的作品是否还在媒体库"。
  - 不在 → 作品级 status = DELETED, 并清 jellyfin_media_id,
    等下次入库时 scanner 重新置 AVAILABLE。
  - 剧集: 作品还在, 但某季在 Jellyfin 里没有实有集了 → 该季 status = DELETED,
    若有非 Specials 季被删则作品 AVAILABLE → PARTIALLY_AVAILABLE,
    并更新 last_season_change。

★★★ 本模块是**全项目唯一**能把状态改成 DELETED 的地方 ★★★
    (scripts/sync_jellyfin 与 scripts/sync_jf_scanner 都不产生 DELETED, 只递增可用性;
     它们的写入一律是纯 upsert / 按剧替换, 从不删除任何行。) 原则是
     「写入可加、删除收敛」。

判定方式(**fail-safe**):
  - 遍历的是**本地** media(**不是**远端列表);
  - 拿本地存的 id **单查**该条目(`GET /Items?ids=` —— 本机 Jellyfin 12.x 对
    `GET /Items/{id}` 一律 400, 实测);
  - **只有"明确查不到"才算消失**; 请求失败/超时/5xx/响应形状异常一律**当作"还在"**;
  - 季级同理: 分集列表**取不到** → 当作"季还在", 不动季级状态。
  - 远端**列表**只用来开快路(命中即"还在"), 但**绝不**用"列表里没有"直接判删。

⚠️ 旧版本在这里加过三道闸(works_ok / a_layer_healthy / seasons_ok), 现已删除:
   闸门是给"拿列表形状判删"打的补丁; 换成"逐个单查 + 失败倒向保留"之后,
   判据本身就安全了, 不再需要闸门(用户 2026-09-22 明确要求不要再叠闸)。

用法:
  python3 scripts/availability_sync.py            # 对账一次
  python3 scripts/availability_sync.py --dry      # 只报告, 不写库
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import load_config
from clients.jellyfin import client as jf
from db.database import SessionLocal, init_db
from db import repositories as repo
from db.models import JellyfinItem, Media, MediaStatus, MediaType


class _Confirmer:
    """作品级"是否还在库"的单查确认器(fail-safe 三态 + 一次运行内缓存)。

    gone(item_id) 只在**明确查不到**时返回 True; 无法判断一律返回 False(当作还在)。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._cache = {}

    async def gone(self, item_id):
        if not item_id:
            return False
        key = str(item_id)
        if key not in self._cache:
            self._cache[key] = await jf.item_exists(self.cfg, key)
        return self._cache[key] is False   # None(不可判断) → False(当作还在)


class _SeasonProbe:
    """单剧"实有季集合"的探测与缓存(fail-safe)。

    known(series_id) -> set(int) | None
        set  = 明确知道这部剧现在有哪几季(可以据此判"某季没了");
        None = **取不到**(请求失败) → 调用方必须不动季级状态。

    ⚠️ 刻意**不用**"全量分集列表"当依据: 那份列表在 Jellyfin 扫库期会残缺,
    拿它的形状判"某季没了"会把实际存在的季误标成已删除, 再去 rollup 把作品降级
    —— 这正是"完整变缺失"的一条路。改为每部剧单独查一次(逐剧取季/分集),
    用缓存保证每部剧一轮只查一次。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._cache = {}

    async def known(self, series_id):
        key = str(series_id or "")
        if not key:
            return None
        if key not in self._cache:
            try:
                eps = await jf.list_episodes_for_series(self.cfg, key)
            except Exception:  # noqa: BLE001
                self._cache[key] = None          # 取不到 → 不可判断(当作季还在)
            else:
                self._cache[key] = {e["season"] for e in eps}
        return self._cache[key]


def _a_layer_maps(session):
    """返回 (eff_tmdb, tmdb_to_jfid) 两张 A 层(jellyfin_item)映射。

    eff_tmdb:     {Jellyfin item_id: 有效 tmdb_id(str)} —— 本地校准映射优先, 否则原挂 ID。
                  用于识别"残留错行": 某 media 行 jfid 指向的 JF item 校准后已归属另一个
                  tmdb_id(如奇门遁甲2 从 615656/巨齿鲨2 改到 1119563), 原错 ID 的行就是残留。
    tmdb_to_jfid: {(tmdb_id(str), 'movie'|'tv'): item_id} —— 供"media 行没记 Jellyfin
                  引用"时反查补齐(见 _run_async 里的说明)。
    """
    from db.models import JellyfinItem
    eff, types = {}, {}
    for iid, tid, typ in session.query(JellyfinItem.item_id, JellyfinItem.tmdb_id,
                                       JellyfinItem.type).all():
        iid = str(iid)
        eff[iid] = str(tid or "")
        if typ:
            types[iid] = typ
    for iid, new_id in session.query(
            repo.TmdbIdCorrection.item_id, repo.TmdbIdCorrection.new_tmdb_id).all():
        if new_id:
            eff[str(iid)] = str(new_id)
    tmdb_to_jfid = {}
    for iid, tid in eff.items():
        if tid and iid in types:
            key = (tid, "movie" if types[iid] == "Movie" else "tv")
            tmdb_to_jfid.setdefault(key, iid)
    return eff, tmdb_to_jfid


async def _run_async(cfg, dry=False):
    init_db()
    live_item_ids = await _live_jellyfin(cfg)
    confirmer = _Confirmer(cfg)
    seasons_probe = _SeasonProbe(cfg)
    stats = {"movie_deleted": 0, "show_deleted": 0, "seasons_deleted": 0,
             "show_demoted": 0, "checked": 0, "kept_unknown": 0,
             "touched": []}          # [(类型, 标题, 原因)] —— dry 时也能看清"会动谁"

    session = SessionLocal()
    try:
        # 只处理 AVAILABLE / PARTIALLY / 库内·完整性未知 的作品
        medias = (session.query(Media)
                  .options(repo.joinedload(Media.seasons))
                  .filter(Media.status.in_((MediaStatus.AVAILABLE,
                                            MediaStatus.PARTIALLY_AVAILABLE,
                                            MediaStatus.OWNED_UNVERIFIED)))
                  .all())
        eff_tmdb, tmdb_to_jfid = _a_layer_maps(session)

        now = datetime.now()

        def _mark(reason):
            """记账: 本条作品本轮被动了什么(标题/类型/原因)。"""
            kind = "电影" if m.media_type == MediaType.MOVIE else "剧集"
            stats["touched"].append((kind, m.title, reason))

        for m in medias:
            # ⚠️ 逐条必提交(try/finally): 旧写法只在"走到循环体末尾"的路径上 commit,
            #    而多处出库判定是"写完就 continue" —— 那条改动随后被 session.close()
            #    丢掉, 于是出现"stats 里报了出库 1 部、界面上那部还在"的假账, 且是否
            #    落库取决于"它是不是本次循环的最后一条", 行为不可预测。
            try:
                stats["checked"] += 1
                jfid = m.jellyfin_media_id
                if not jfid:
                    # 没有记 Jellyfin 引用(多为历史行 / OWNED_UNVERIFIED=8): 用 A 层按
                    # (tmdb_id, 类型) 反查补齐再判定。不做这步的话这些行会被**永久跳过** ——
                    # 作品早已从 Jellyfin 删除, 本地却一直显示"库内"(实测: 法证先锋/法证先锋2
                    # 等 8 行长期卡在 status=8, 反向对账完全失效)。
                    jfid = tmdb_to_jfid.get((str(m.tmdb_id), str(m.media_type)))
                if not jfid:
                    # A 层里也没有这部作品 → 它确实不在 Jellyfin 里 → 出库。
                    # ⚠️ 这个判据现在可信: A 层同步是**纯 upsert**(只增改、从不清空/删行),
                    #    所以"A 层没有"意味着 Jellyfin 侧确实没有(旧写法里 A 层会被残缺
                    #    快照洗小, 那时这个判据会把在库作品误删 —— 所以现在不需要再加闸)。
                    if m.status in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE,
                                    MediaStatus.OWNED_UNVERIFIED):
                        stats["movie_deleted" if m.media_type == MediaType.MOVIE
                               else "show_deleted"] += 1
                        _mark("A 层无此作品(确实不在 Jellyfin)")
                        if not dry:
                            m.status = MediaStatus.DELETED
                            m.jellyfin_media_id = None
                    continue
                # ── 作品级: 先走快路(在库条目列表里命中 = 还在), 未命中才单查确认 ──
                exists = jfid in live_item_ids
                if not exists:
                    if await confirmer.gone(jfid):
                        pass                      # 明确查不到 → 下面按"消失"处理
                    else:
                        exists = True             # 有 id 但单查给不出"明确没有" → 当作还在
                        stats["kept_unknown"] += 1
                # 残留错行: 该 JF item 校准后已归属另一个 tmdb_id → 本行是旧错 ID, DELETED
                eff = eff_tmdb.get(jfid)
                if eff and eff != str(m.tmdb_id):
                    if not dry:
                        m.status = MediaStatus.DELETED
                        m.jellyfin_media_id = None
                    stats["movie_deleted" if m.media_type == MediaType.MOVIE
                           else "show_deleted"] += 1
                    _mark(f"残留错行: JF条目 {jfid[:12]}… 实属另一 TMDB id")
                    continue
                if m.media_type == MediaType.MOVIE:
                    if not exists and m.status == MediaStatus.AVAILABLE:
                        stats["movie_deleted"] += 1
                        _mark(f"单查确认已不在库(id {jfid[:12]}…)")
                        if not dry:
                            m.status = MediaStatus.DELETED
                            m.jellyfin_media_id = None  # 清库内 id, 等下次入库重写
                    continue
                # 剧集
                if not exists:
                    # 作品不在库 → 整部 DELETED
                    if m.status in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE,
                                    MediaStatus.OWNED_UNVERIFIED):
                        stats["show_deleted"] += 1
                        _mark(f"单查确认已不在库(id {jfid[:12]}…)")
                        if not dry:
                            m.status = MediaStatus.DELETED
                            m.jellyfin_media_id = None
                    continue
                # 作品在库 → 若"库内·完整性未知"(8), 状态由 scanner 负责, 跳过对账(避免
                # _recompute_show_status 对空季误判成 PROCESSING 把 8 洗掉)
                if m.status == MediaStatus.OWNED_UNVERIFIED:
                    continue
                # 作品在库 → 逐季对账
                # ⚠️ 分集列表**取不到**(known() 返回 None)时不动季级状态: 拿残缺数据
                #    判"某季没了", 会把实际存在的季误标成已删除, 再去 rollup 把作品降级
                #    (AVAILABLE → PARTIALLY_AVAILABLE), 这也是"完整变缺失"的一条路。
                present = await seasons_probe.known(jfid)
                if present is None:
                    continue
                changed = False
                for s in m.seasons:
                    if s.status in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE) \
                            and s.season_number not in present:
                        stats["seasons_deleted"] += 1
                        _mark(f"第 {s.season_number} 季已不在库")
                        if not dry:
                            s.status = MediaStatus.DELETED
                            changed = True
                if not dry and changed:
                    # 有季被移除 → 完整作品降为"部分可用"。
                    # ⚠️ 刻意**不**做作品级 rollup: rollup 在"所有季都没了"时会得出
                    #    UNKNOWN(1), 而 UNKNOWN 在 UI 上等于"未拥有 / 缺失" —— 那就把
                    #    一部**还在库里**的剧显示成缺失了(正是"完整变缺失"的另一种形态)。
                    #    这里只做"降级"这一个动作, 作品级的移除判定归"作品不在库"那条路径。
                    if m.status == MediaStatus.AVAILABLE:
                        m.status = MediaStatus.PARTIALLY_AVAILABLE
                        stats["show_demoted"] += 1
                        _mark("有季被移除 → 降级 AVAILABLE→PARTIALLY_AVAILABLE")
                    m.last_season_change = now
            finally:
                if not dry:
                    session.commit()
        return stats
    finally:
        session.close()


async def _live_jellyfin(cfg):
    """返回当前 Jellyfin 里真实存在的 Movie/Series item_id 集合(作品级**快路**)。

    ⚠️ 这只是快路: 命中它可以直接判定"还在库"; 但**没命中不能直接判删** —— 必须再按
    ID 单查确认(见 _Confirmer)。Jellyfin 扫库期返回的列表是残缺的, 拿列表的"形状"
    当删除依据正是旧 bug 的来源(实测《鬼语者》107 集只回 24 集)。
    """
    items = await jf.list_items(cfg)
    return {str(it["item_id"]) for it in items if it.get("item_id")}


def run(dry=False):
    """可用性对账入口。持全局同步锁(与条目同步/可用性扫描互斥, 共同写 media/season)。"""
    from lib.sync_guard import SYNC_LOCK
    cfg = load_config()
    if not cfg.get("jellyfin", {}).get("url"):
        raise RuntimeError("未配置 jellyfin.url / jellyfin.token(管理 → 通用 → Jellyfin)")
    with SYNC_LOCK:                      # 见 lib/sync_guard.py
        return asyncio.run(_run_async(cfg, dry=dry))


def main():
    ap = argparse.ArgumentParser(description="可用性对账")
    ap.add_argument("--dry", action="store_true", help="只报告, 不写库")
    args = ap.parse_args()
    try:
        stats = run(dry=args.dry)
        touched = stats.pop("touched", [])
        print(f"可用性对账{'(dry)' if args.dry else ''}完成: {stats}")
        if touched:
            print(f"  ── 本轮判定「已出库 / 降级」的 {len(touched)} 条 "
                  f"({'仅报告, 未写库' if args.dry else '已写库'}):")
            for kind, title, reason in touched[:50]:
                print(f"     · [{kind}] {title} —— {reason}")
            if len(touched) > 50:
                print(f"     … 另有 {len(touched) - 50} 条")
        else:
            print("  ── 无需出库/降级的作品, 库内作品全部对得上。")
    except Exception as e:  # noqa: BLE001
        print(f"对账失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
