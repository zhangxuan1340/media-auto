#!/usr/bin/env python3
"""回填 tmdb_media 剧集行的 certification + keywords_json(一次性修复脚本)
=========================================================================

背景(2026-09-22 TMDB 数据完整性审计):
    TMDB 的电影与剧集把同一类数据放在**不同字段**里, 而 clients/tmdb/client.py
    的 detail() 早期只按电影口径取值, 导致剧集两个字段恒空:

      ① 分级: 电影在 /movie/{id}/release_dates(append "release_dates"),
              剧集在 /tv/{id}/content_ratings (append "content_ratings")。
              detail() 只 append release_dates → 剧集 certification 恒 "";
      ② 关键词: 电影在 keywords.keywords, 剧集在 keywords.results。
              detail() 只读 keywords.keywords → 剧集 keywords 恒空。

    影响:
      · 写出的 tvshow.nfo 的 <mpaa>/<certification>/<tag> 恒空
        (NFO 里剧集分级/标签永远缺失, Jellyfin/TMM 侧看不到分级与标签 ——
         而 Jellyfin 正是靠分级做访问控制/显示控制的依据);
      · 剧集分级缺失时, 本地缓存路径(organize.resolve_from_local_cache)的 18+ 判定
        也会只剩关键词/adult 标志(该判定 2026-09-22 已随 Ts 分类一起移除, 此处仅记历史)。

    detail() 已修(见下方"修复"), 但**存量行**是修复前写库的, 需本脚本回填。

修复(代码侧, 已在 clients/tmdb/client.py 落地):
    ① 新增 _certification_tv(content_ratings) → "US:TV-MA / HK:M" 风格;
    ② detail() 剧集 append "content_ratings", certification 按 kind 分派;
    ③ detail() 关键词同时兼容 keywords.keywords 与 keywords.results。

本脚本(数据侧):
    只重拉"certification 为空"的行(收敛范围, 不全网重拉)—— 就是全部剧集存量行;
    顺便补齐 keywords_json(与新库同口径)。

用法:
    ./venv/bin/python scripts/backfill_tmdb_tv_meta.py             # 回填全部空分级剧集
    ./venv/bin/python scripts/backfill_tmdb_tv_meta.py --kind movie # 也允许 movie(cert 空多为真无)
    ./venv/bin/python scripts/backfill_tmdb_tv_meta.py --dry-run   # 只统计不写
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONCURRENCY = 8          # 与 sync_tmdb 同量级, TMDB 限流 ~50 req/s, 8 并发很安全


async def main(kind: str, dry: bool, limit: int) -> int:
    from db.database import SessionLocal, init_db
    from db import models as M
    from db import repositories as repo
    from lib.config import load_config
    from clients.tmdb import client as tmdb

    cfg = load_config()
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        print("config.json 未配置 tmdb.api_key")
        return 1
    init_db()

    session = SessionLocal()
    try:
        q = session.query(M.TmdbMedia).filter(
            M.TmdbMedia.kind == kind,
            M.TmdbMedia.certification.is_(None) | (M.TmdbMedia.certification == ""))
        if limit:
            q = q.limit(limit)
        ids = [(r.tmdb_id, r.title) for r in q.all()]
    finally:
        session.close()

    total = len(ids)
    print(f"[backfill-tv] kind={kind} 待回填 {total} 条(并发 {CONCURRENCY})")
    if not total or dry:
        return 0

    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    stats = {"ok": 0, "fail": 0, "cert": 0, "kw": 0, "n": 0}
    t0 = time.time()

    async def worker(item):
        tid, title = item
        async with sem:
            try:
                d = await tmdb.detail(cfg, kind, tid)
            except Exception as e:  # noqa: BLE001
                async with lock:
                    stats["fail"] += 1
                    if stats["fail"] <= 5:
                        print(f"  ! {kind}/{tid} {title}: {type(e).__name__}: {e}")
                return
            cert = (d.get("certification") or "").strip() if d else ""
            kws = json.dumps(d.get("keywords") or [], ensure_ascii=False) if d else ""
            async with lock:
                stats["n"] += 1
                s2 = SessionLocal()
                try:
                    obj = repo.get_tmdb_media_by_id(s2, kind, tid)
                    if obj is not None:
                        if cert:
                            obj.certification = cert
                            stats["cert"] += 1
                        if kws and kws != "[]":
                            obj.keywords_json = kws
                            stats["kw"] += 1
                        s2.commit()
                        stats["ok"] += 1
                except Exception:  # noqa: BLE001
                    s2.rollback()
                    stats["fail"] += 1
                finally:
                    s2.close()
                if stats["n"] % 100 == 0 or stats["n"] >= total:
                    print(f"[backfill-tv] 进度 {stats['n']}/{total} "
                          f"(成功 {stats['ok']} 失败 {stats['fail']} 补分级 {stats['cert']} 补关键词 {stats['kw']}) "
                          f"用时 {time.time()-t0:.0f}s", flush=True)

    await asyncio.gather(*[worker(it) for it in ids])
    print(f"[backfill-tv] 完成: 成功 {stats['ok']} / 失败 {stats['fail']} / "
          f"补到分级 {stats['cert']} / 补到关键词 {stats['kw']}, 用时 {time.time()-t0:.0f}s")
    return 0 if stats["fail"] == 0 else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="回填 tmdb_media 的 certification / keywords_json")
    ap.add_argument("--kind", default="tv", choices=["tv", "movie"], help="条目类型(默认 tv)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条(0=全部)")
    ap.add_argument("--dry-run", action="store_true", help="只统计待回填条数")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.kind, a.dry_run, a.limit)))
