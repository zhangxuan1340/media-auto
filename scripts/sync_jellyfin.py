#!/usr/bin/env python3
"""
media-auto / sync_jellyfin —— 把 Jellyfin 数据同步到本地 SQLite
============================================================
从 Jellyfin 拉取两类数据写入本地库(data/media_auto.db):
  - 媒体库:    GET /Library/VirtualFolders
  - 媒体项:    GET /Items (电影/剧集,含路径与 ProviderIds)

⚠️ **写入模型(2026-09-22 定稿)**: 本模块所有写入都是**纯 upsert**
   —— 新增缺失的行、更新变化的行, **从不删除任何行**。
   设计约定: 全类**没有任何**
   delete/clear/truncate; 扫描没看到的条目就只是"没被更新"。
   这样一来, 一份**残缺的远端快照**(Jellyfin 正在扫库时会发生, 实测《鬼语者》
   107 集只回 24 集)最多让本地**少写几条**, 下一轮/明天就补上, **绝不会**把
   已有的正确数据洗成缺失数据。
   「某个作品是否已从库里消失」只由 `scripts/availability_sync.py` 判定
   (按 ID 单查 + 非 404 一律当"还在"), 它是全项目**唯一**能把状态改成 DELETED 的地方。

⚠️ items 同步**不用 DateCreated 游标**, 而是全库比对(Jellyfin 侧只几页请求)。
   原因: DateCreated 取自文件/扫描时间、不保证单调 —— 后入库的条目可能带更早的
   时间戳, 游标越过去就永久漏条目。详见 _sync_items()。

用法:
  python3 scripts/sync_jellyfin.py --scope all        # 媒体库 + 媒体项(全库比对)
  python3 scripts/sync_jellyfin.py --scope libraries
  python3 scripts/sync_jellyfin.py --scope items      # 媒体项(全库比对, 只写差异)
  python3 scripts/sync_jellyfin.py --scope items --full   # 媒体项(强制重写每一条)
  python3 scripts/sync_jellyfin.py --scope episodes   # 分集明细(全量拉取, 按剧差量替换)
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import load_config
from clients.jellyfin import client as jellyfin_client
from db.database import SessionLocal, init_db
from db.models import JellyfinItem
from db import repositories as repo


# 参与"是否变化"比对的字段(entry 与模型列同名)
_DIFF_FIELDS = ("library_id", "name", "type", "year", "path",
                "tmdb_id", "imdb_id", "overview", "date_created")


def _naive(dt):
    """去掉 tzinfo 后再比: SQLite 存 DateTime 会丢时区, 直接比会永远判不等。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def _same(row, it):
    """本地行与 Jellyfin 条目是否等价(只比参与同步的字段)。"""
    for k in _DIFF_FIELDS:
        a = getattr(row, k)
        b = it.get(k)
        if k == "date_created":
            if _naive(a) != _naive(b):
                return False
        elif (a or "") != (b or "") and a != b:
            return False
    return True


async def _sync_items(cfg, session, full: bool = False) -> int:
    """条目(jellyfin_item)同步。返回写入条数。**纯 upsert, 不删任何行。**

    full=False(默认, 调度器每 5 分钟跑): **全库比对** —— 拉全库条目, 与本地逐条比,
      只写真正有变化的行。
    full=True: 同样拉全库, 但**强制重写**每一条(用于修数据 / 回校)。

    为什么不用 DateCreated 增量游标: Jellyfin 的 DateCreated 取自文件/扫描时间,
    **不保证单调** —— 后入库的条目可能带上更早的时间戳。游标一旦越过该时间戳,
    这条就永久漏掉。全库比对只有几页请求、写库只写差异行, 代价可忽略且绝对不漏。

    ⚠️⚠️ 已删除的两个危险动作(2026-09-22 定稿):
      · full 时的 `wipe_jellyfin_items`(清空整表再重建);
      · 全库比对时按 `stale_ids` 删除"Jellyfin 列表里没有"的本地行。
    两者都用**远端列表的形状**去决定删本地数据 —— 而这份列表在 Jellyfin 扫库期是
    残缺的, 于是一次同步就能把完整镜像洗成残缺镜像, 下游可用性对账跟着全线误判
    (把在库作品判成"未拥有")。绝不能用列表形状删东西。
    本地多出来的行现在一律**保留**: 它可能是 Jellyfin 侧真删了(那由 availability_sync
    按 ID 单查后判出库), 也可能只是这份列表残缺 —— 分不清就不动。
    """
    items = await jellyfin_client.list_items(cfg)

    if full:
        for it in items:
            repo.upsert_jellyfin_item(session, it)
        session.commit()
        print(f"[jellyfin] 全量模式: 已写入本地条目 {len(items)} 个"
              f"(纯 upsert, 未清空、未删除任何行)")
        return len(items)

    existing = {r.item_id: r for r in session.query(JellyfinItem).all()}
    added = updated = 0
    for it in items:
        row = existing.get(it.get("item_id") or "")
        if row is None:
            repo.upsert_jellyfin_item(session, it)
            added += 1
        elif not _same(row, it):
            repo.upsert_jellyfin_item(session, it)
            updated += 1
    session.commit()
    print(f"[jellyfin] 媒体项全库比对: 共 {len(items)} 个 → 新增 {added} / 更新 {updated}"
          f"(本地共 {len(existing)} 行, 未删除任何行)")
    return added + updated


async def run(scope: str = "all", full: bool = False) -> int:
    """同步入口(供 CLI 与 server 复用)。持全局同步锁, 与其它写库的同步任务互斥。

    items 同步默认【全库比对】: 拉全库条目与本地逐条比, 只写有变化的行。
    full=True 时强制重写每一条(仍然只 upsert, 不清表)。
    """
    from lib.sync_guard import SYNC_LOCK
    with SYNC_LOCK:                      # 见 lib/sync_guard.py(可重入, 调度器会整体持锁)
        return await _run_sync(scope, full=full)


async def _run_sync(scope: str, full: bool) -> int:
    """同步主体(调用方须已持 SYNC_LOCK)。"""
    cfg = load_config()
    if not cfg.get("jellyfin", {}).get("url"):
        raise RuntimeError("config.json 未配置 jellyfin.url / jellyfin.token")

    init_db()
    session = SessionLocal()
    total = 0
    try:
        # 媒体库(顺带在 items 流程里也刷新一次,保证 library_id 可关联)
        if scope in ("all", "libraries", "items"):
            libs = await jellyfin_client.list_libraries(cfg)
            for lib in libs:
                lib["item_count"] = await jellyfin_client.library_item_count(cfg, lib["library_id"])
                repo.upsert_jellyfin_library(session, lib)
            session.commit()
            total += len(libs)
            print(f"[jellyfin] 媒体库: {len(libs)} 个")

        if scope in ("all", "items"):
            total += await _sync_items(cfg, session, full=full)

            # 全量同步会用 Jellyfin 的 ProviderIds 覆盖本地 tmdb_id, 而 Jellyfin 里
            # 有些条目挂错 TMDb ID(刮错/同名撞号)。同步后立刻用 TMDB 接口回校,
            # 把这些错 ID 再修正 —— 否则 inLibrary 会重新误判"缺失"。
            if full:
                try:
                    from scripts import calibrate_jf_ids
                    res, fixes, _mis = calibrate_jf_ids.run_calibration(
                        cfg, items=None, apply=True, with_jf=False)
                    if res.get("fix"):
                        print(f"[jellyfin] 全量同步后已自动回校 TMDb ID, 修正 {res['fix']} 条"
                              f"(一致 {res.get('ok', 0)})")
                    elif res.get("ok"):
                        print(f"[jellyfin] 全量同步后回校: ID 均一致({res.get('ok')}), 无需修正")
                except Exception as e:  # noqa: BLE001
                    print(f"[jellyfin] 全量同步后回校失败(不影响同步): {e}")

        if scope == "episodes":
            # 分集明细: 全量拉取(~7.7 万条, 约 4 分钟)后**按剧差量替换**。
            # ⚠️ 这是全链路曾经最危险的一处: 旧写法 wipe 后重建, 一份残缺列表就能
            #    把所有剧的分集一起踩少。现在不做任何清空动作 —— 只对"这份快照里
            #    出现的剧"逐剧替换, 没覆盖到的剧原样保留(见 _replace_episodes_by_series)。
            print("[jellyfin] 分集明细: 开始全量拉取(Episode)...")
            eps = await jellyfin_client.list_episodes(cfg)
            n_series, n_eps = repo.replace_jf_episodes_grouped(session, eps)
            session.commit()
            total += n_eps
            print(f"[jellyfin] 分集明细: {n_eps} 集 / {n_series} 部剧"
                  f"(按剧替换, 未清空整表)")
    finally:
        session.close()
    return total


def main():
    ap = argparse.ArgumentParser(description="同步 Jellyfin 数据到本地 SQLite")
    ap.add_argument("--scope", choices=["all", "libraries", "items", "episodes"], default="all")
    ap.add_argument("--full", action="store_true",
                    help="全库比对并强制重写每一条(默认: 只写差异行)。仍不清表、不删行")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    try:
        n = asyncio.run(run(args.scope, full=args.full))
        print(f"Jellyfin 同步完成,共 {n} 条。")
    except Exception as e:
        print(f"Jellyfin 同步失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
