#!/usr/bin/env python3
"""回填 tmdb_media.countries(一次性修复脚本)
=========================================

背景(2026-09-22 《鼠胆龙威》Bug):
    整理页反查有两条路径 —— 本地 TMDB 缓存(tmdb_media)与 TMDB 直连。老表的
    tmdb_media 没有 countries 列, 本地路径只能靠 original_language 判地区; 而港片的
    original_language 常是 "cn"(国语配音版) → 本地路径把港片判成 CnMovie,
    TMDB 直连路径(带 production_countries=['HK'])判成 HkMovie。
    同一部片两次整理, 第一次(未进缓存)→HkMovie, 第二次(已进缓存)→CnMovie。

修复:
    ① 模型加了 countries 列(新数据由 sync_tmdb 自动写入);
    ② 本脚本把**存量行**的 countries 从 TMDB 补齐。

只补"会影响分类结果"的行(收敛范围, 不全网重拉):
    classify._region 的优先级是 yue/HK/TW(国家) > 语言; 也就是说 countries 只在
    "语言判出的地区 ≠ HK/TW, 但 production_countries 含 HK/TW" 时才会改变结果 ——
    即语言为 cn/yue/zh 的条目(可能来自大陆/港/台)。其它语言(en/ja/ko…)语言与
    国家给出的地区一致, 补了也不改变分类, 不值得花 API 配额。

用法:
    ./venv/bin/python scripts/backfill_tmdb_countries.py            # 只补 cn/yue/zh
    ./venv/bin/python scripts/backfill_tmdb_countries.py --all      # 全量(慎用, ~6300 次 API)
    ./venv/bin/python scripts/backfill_tmdb_countries.py --dry-run  # 只统计不写
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONCURRENCY = 8          # 与 sync_tmdb 同量级, TMDB 限流 ~50 req/s, 8 并发很安全
# 只有这些语言的电影, countries 才可能改变 classify 结果(HK/TW 优先于语言)
AMBIG_LANGS = ("cn", "yue", "zh")


async def main(all_rows: bool, dry: bool) -> int:
    from db.database import SessionLocal, init_db
    from db import models as M
    from db import repositories as repo
    from lib.config import load_config
    from clients.tmdb import client as tmdb

    cfg = load_config()
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        print("未配置 tmdb.api_key(管理 → 通用 → TMDB)")
        return 1
    init_db()   # 幂等: 给老表补 countries 列

    session = SessionLocal()
    try:
        q = session.query(M.TmdbMedia).filter(
            M.TmdbMedia.countries.is_(None) | (M.TmdbMedia.countries == ""))
        if not all_rows:
            q = q.filter(M.TmdbMedia.original_language.in_(AMBIG_LANGS))
        rows = q.all()
    finally:
        session.close()

    total = len(rows)
    print(f"[backfill] 待回填 {total} 条"
          f"({'全部' if all_rows else '语言 cn/yue/zh'}, 并发 {CONCURRENCY})")
    if not total or dry:
        return 0

    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    stats = {"ok": 0, "fail": 0, "hk": 0, "n": 0}
    t0 = time.time()

    async def worker(r):
        async with sem:
            try:
                d = await tmdb.detail(cfg, r.kind, r.tmdb_id)
                countries = ",".join(str(c) for c in (d.get("countries") or []))
            except Exception as e:  # noqa: BLE001
                async with lock:
                    stats["fail"] += 1
                    if stats["fail"] <= 5:
                        print(f"  ! {r.kind}/{r.tmdb_id} {r.title}: {type(e).__name__}: {e}")
                return
            async with lock:
                stats["n"] += 1
                s2 = SessionLocal()
                try:
                    obj = repo.get_tmdb_media_by_id(s2, r.kind, r.tmdb_id)
                    if obj is not None:
                        obj.countries = countries
                        s2.commit()
                        stats["ok"] += 1
                        if "HK" in countries or "TW" in countries:
                            stats["hk"] += 1
                except Exception:  # noqa: BLE001
                    s2.rollback()
                    stats["fail"] += 1
                finally:
                    s2.close()
                if stats["n"] % 100 == 0 or stats["n"] >= total:
                    print(f"[backfill] 进度 {stats['n']}/{total} "
                          f"(成功 {stats['ok']} 失败 {stats['fail']} 港台 {stats['hk']}) "
                          f"用时 {time.time()-t0:.0f}s", flush=True)

    await asyncio.gather(*[worker(r) for r in rows])
    print(f"[backfill] 完成: 成功 {stats['ok']} / 失败 {stats['fail']} / "
          f"其中港台(HK/TW) {stats['hk']} 条, 用时 {time.time()-t0:.0f}s")
    return 0 if stats["fail"] == 0 else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="回填 tmdb_media.countries")
    ap.add_argument("--all", action="store_true", help="全量回填(不只 cn/yue/zh)")
    ap.add_argument("--dry-run", action="store_true", help="只统计待回填条数")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.all, a.dry_run)))
