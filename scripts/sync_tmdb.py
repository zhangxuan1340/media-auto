#!/usr/bin/env python3
"""
media-auto / sync_tmdb —— 把 TMDB 元数据同步到本地 SQLite
================================================================================
种子来源 = 本地 Jellyfin 库(jellyfin_item 表, 已由增量同步维护)。
对每个 tmdb_id 调 TMDB 官方 API 拉 详情(+演员/类型/剧情) 写入 tmdb_media;
剧集额外拉全部季的分集结构写入 tmdb_season(供「分集级缺失」精确对比)。

增量策略: 只同步"本地缓存里没有 / 超过 refresh_days 天没刷"的条目(full=全部重刷)。
无 tmdb.api_key 时直接报错退出(上层会提示去配置)。

并发模型: 网络拉取并发(CONCURRENCY), 数据库写入串行(单 writer 协程从队列消费),
避免多线程共享 SQLAlchemy session。

用法:
  python3 scripts/sync_tmdb.py                # 增量
  python3 scripts/sync_tmdb.py --full         # 全量重刷
  python3 scripts/sync_tmdb.py --limit 20     # 只同步 20 条(调试)
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import load_config
from clients.tmdb import client as tmdb
from lib import titles
from db.database import SessionLocal, init_db
from db import repositories as repo
from db.models import JellyfinItem, TmdbMedia

CONCURRENCY = 5          # 并发拉取(TMDB 限速 ~10 req/s, 5 并发安全)
REFRESH_DAYS = 30        # 超过多少天没刷的条目, 下次增量时重刷
COMMIT_BATCH = 50        # 攒 50 条落一次盘(旧: 每条一次 commit+fsync)


def _seed_ids(session, limit=None):
    """本地 Jellyfin 库里的 (kind, tmdb_id) 种子集合。Movie→movie, Series→tv。"""
    qry = session.query(JellyfinItem.tmdb_id, JellyfinItem.type).filter(
        JellyfinItem.tmdb_id != "")
    seen, out = set(), []
    for tid, typ in qry.all():
        kind = "movie" if typ == "Movie" else "tv" if typ == "Series" else None
        if not kind:
            continue
        try:
            key = (kind, int(tid))
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if limit and len(out) >= limit:
            break
    return out


def _needs_refresh(session, kind, tmdb_id, full, refresh_days):
    row = repo.get_tmdb_media_by_id(session, kind, tmdb_id)
    if row is None:
        return True
    if full:
        return True
    if not row.synced_at:
        return True
    return (datetime.now() - row.synced_at) > timedelta(days=refresh_days)


def _stale_keys(session, seeds, full, refresh_days):
    """从一批种子里挑出要刷新的 (kind, tmdb_id) —— 一次查询代替逐条 _needs_refresh。

    旧写法: 4444 个种子各发一条 `get_tmdb_media_by_id`(4444 次往返)。批量只需
    一次 `IN` 查询(~0.4ms), 判定口径与 _needs_refresh 完全一致:
    本地没有 / 没有 synced_at / 超过 refresh_days 都算"该刷"。
    """
    if full:
        return list(seeds)
    ids = sorted({int(t) for _k, t in seeds})
    if not ids:
        return []
    # 只取三列: kind + tmdb_id + synced_at(全表 ORM 会把 overview/海报/cast_json 都拖出来)
    rows = (session.query(TmdbMedia.kind, TmdbMedia.tmdb_id, TmdbMedia.synced_at)
            .filter(TmdbMedia.tmdb_id.in_(ids))
            .all())
    fresh = {(k, t): sa for k, t, sa in rows}
    now = datetime.now()
    out = []
    for key in seeds:
        sa = fresh.get(key)
        if sa is None or (now - sa) > timedelta(days=refresh_days):
            out.append(key)
    return out


def _image_urls_from_row(row: dict):
    """从一行 tmdb_media 数据里抽出所有图片 URL(封面/背景/演员头像)。"""
    urls = set()
    for f in ("poster", "backdrop"):
        v = row.get(f)
        if v and v.startswith("http"):
            urls.add(v)
    try:
        for c in json.loads(row.get("cast_json") or "[]"):
            t = c.get("thumb")
            if t and t.startswith("http"):
                urls.add(t)
    except Exception:  # noqa: BLE001
        pass
    return urls


def _image_urls_from_media(obj):
    """从已持久化的 TmdbMedia ORM 对象抽图片 URL。"""
    urls = set()
    for f in ("poster", "backdrop"):
        v = getattr(obj, f, "")
        if v and v.startswith("http"):
            urls.add(v)
    try:
        for c in json.loads(obj.cast_json or "[]"):
            t = c.get("thumb")
            if t and t.startswith("http"):
                urls.add(t)
    except Exception:  # noqa: BLE001
        pass
    return urls


def _media_row_from_detail(meta, english_title=""):
    """tmdb.detail() 返回 → tmdb_media 行字段。english_title 由调用方另行拉取传入。"""
    return {
        "title": meta.get("title", ""),
        "original_title": meta.get("originalTitle", ""),
        "english_title": english_title,
        # 多语言中文译名(繁/台/港), 逗号分隔落库, 供磁力多标题匹配(2026-10-04)
        "alt_titles": ",".join(meta.get("altTitles") or []),
        "year": meta.get("year", ""),
        "overview": meta.get("overview", ""),
        "poster": meta.get("poster", ""),
        "backdrop": meta.get("backdrop", ""),
        "vote": meta.get("vote", 0.0),
        "imdb_id": meta.get("imdb_id", ""),
        "tvdb_id": meta.get("tvdb_id") or 0,
        "status": meta.get("status", ""),
        "in_production": meta.get("in_production", False),
        "original_language": meta.get("original_language", ""),
        # 产地国家(逗号分隔 ISO 码) —— organize 本地缓存反查的分类依赖它判港台(Hk),
        # 缺了会退化成按语言判(港片语言常是 cn → 误进 CnMovie, 2026-09-22 修)。
        "countries": ",".join(str(c) for c in (meta.get("countries") or [])),
        "genres": ",".join(str(g) for g in (meta.get("genre_ids") or [])),
        "genre_names": ",".join(meta.get("genres") or []),
        "cast_json": json.dumps(meta.get("cast") or [], ensure_ascii=False),
        "directors_json": json.dumps(meta.get("directors") or [], ensure_ascii=False),
        "studios_json": json.dumps(meta.get("studios") or [], ensure_ascii=False),
        "keywords_json": json.dumps(meta.get("keywords") or [], ensure_ascii=False),
        "certification": meta.get("certification", ""),
        "runtime": meta.get("runtime") or 0,
        "premiered": meta.get("premiered", ""),
        "end_date": meta.get("end_date", ""),
        "number_of_seasons": meta.get("number_of_seasons") or 0,
    }


async def _fetch_one(cfg, kind, tmdb_id):
    """拉一条(详情 + 剧集季结构)。成功返回写库用的 dict, 失败返回 None。重试 1 次。"""
    meta = None
    for _ in range(2):
        try:
            meta = await tmdb.detail(cfg, kind, tmdb_id)
            if meta:
                break
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1.0)
    if not meta:
        return None
    seasons = []
    if kind == "tv":
        # detail() 本身就带 seasons(同结构, 见 clients/tmdb.detail), 旧写法再调一次
        # all_seasons = 把同一个 /3/tv/{id} 整个重拉一遍: 全量同步时剧集侧白烧一倍
        # TMDB 配额和一次往返。只有 detail 没给到(异常/空)才退回单拉。
        seasons = meta.get("seasons") or []
        if not seasons:
            try:
                seasons = await tmdb.all_seasons(cfg, tmdb_id) or []
            except Exception:  # noqa: BLE001
                seasons = []
    # 英文标题(供磁力双查): 原语言本就是英文 → 直接复用 title(省一次 API);
    # 否则(如中文片)单独拉 ?language=en 的标题, 失败不阻断(留空)。
    if (meta.get("original_language") or "") == "en":
        english_title = meta.get("title") or ""
    else:
        try:
            english_title = await tmdb.english_title(cfg, kind, tmdb_id)
        except Exception:  # noqa: BLE001
            english_title = ""
    return {"kind": kind, "tmdb_id": tmdb_id,
            "row": _media_row_from_detail(meta, english_title), "seasons": seasons,
            # TMDB 大陆译名为空时的台/港译名 —— apply_to_row 拿它当"豆瓣也查不到"的退路
            "zh_fallback": meta.get("zh_fallback") or ""}


class GhostMediaError(Exception):
    """TMDB 幽灵条目(未上映、无年份/海报/评分)。/pull 捕获后回 404, 不落库。"""


def _is_ghost_row(row: dict) -> bool:
    """TMDB 幽灵条目: 无年份(未上映)+ 无海报 + 零评分 —— 通常是同名系列里
    未上映的第二部(例: 「保镖恋人」的 Ébano 版)。这类条目没有可看的实体,
    写进本地缓存只会污染浏览/搜索/缺失页(显示成"在库但空白"的假数据)。"""
    return not row.get("year") and not row.get("poster") and not (row.get("vote") or 0)


async def sync_one(kind: str, tmdb_id: int) -> bool:
    """按 (kind, tmdb_id) 单独拉一条 TMDB 详情写库(含剧集季结构)。
    供 Web 详情弹窗"本地没有 → 现拉并入库"兜底, 与 run() 共用 _fetch_one。
    返回是否写入成功。

    ⚠️ 根因防护(2026-09-17 修"查看即污染"bug): 查看一个本地没有的条目会触发
    /pull 写库 —— 若 TMDB 上是幽灵条目(未上映第二部, 无年份/无海报/零评分),
    写库就会把假数据固化进本地缓存, 之后浏览/搜索/缺失页都会出现这条
    "在库但空白"的记录。故拉取到幽灵条目时**拒绝写库**, 返回 False。
    """
    cfg = load_config()
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise RuntimeError("未配置 tmdb.api_key(管理 → 通用 → TMDB)")
    init_db()
    item = await _fetch_one(cfg, kind, tmdb_id)
    if not item:
        return False
    if _is_ghost_row(item["row"]):
        # 幽灵条目不落库: 抛异常让 /pull 回 404(前端显示"TMDB 无此条目")。
        # 不写库 = 下次查看仍是 404, 不产生"在库但空白"的假数据。
        raise GhostMediaError(f"{kind}/{tmdb_id} 是 TMDB 幽灵条目(无年份/海报/评分), 拒绝缓存")
    session = SessionLocal()
    try:
        old_obj = repo.get_tmdb_media_by_id(session, kind, tmdb_id)
        old_imgs = _image_urls_from_media(old_obj) if old_obj else set()
        new_imgs = _image_urls_from_row(item["row"])
        # 中文标题兜底: 手动覆盖 > TMDB > 豆瓣(国内译名)> TMDB 台/港译名
        titles.apply_to_row(cfg, kind, item["row"], old_obj, item.get("zh_fallback") or "")
        repo.upsert_tmdb_media(session, item)
        if item.get("seasons"):
            repo.upsert_tmdb_seasons(session, tmdb_id, item["seasons"])
        session.commit()
        stale = old_imgs - new_imgs
        if stale:
            try:
                from server import imgproxy
                for u in stale:
                    imgproxy.invalidate(u)
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()


async def run(scope: str = "all", full: bool = False, limit: int = 0,
              refresh_days: int = REFRESH_DAYS) -> int:
    """执行 TMDB 同步, 返回成功同步条目数。可被 CLI 与 server 复用。

    并发模型: 用信号量控制并发拉取(CONCURRENCY), 拉到的结果**先攒在内存**,
    拉完一批后串行写库(session 单线程)。不用 asyncio.Queue(之前 sentinel 收尾
    有 task_done 竞态)。4444 条元数据内存占用可忽略。
    """
    cfg = load_config()
    if not (cfg.get("tmdb", {}) or {}).get("api_key"):
        raise RuntimeError("未配置 tmdb.api_key(管理 → 通用 → TMDB;免费注册: themoviedb.org → Settings → API)")

    init_db()
    session = SessionLocal()
    t0 = time.time()
    stats = {"ok": 0, "fail": 0, "n": 0}
    try:
        seeds = _seed_ids(session, limit=limit or None)
        todo = _stale_keys(session, seeds, full, refresh_days)   # 一次 IN 查询, 不逐条查
        total = len(todo)
        print(f"[tmdb] 种子 {len(seeds)} 个, 待同步 {total} 个"
              f"({'全量' if full else '增量'}, 并发 {CONCURRENCY})")
        if not total:
            return 0

        sem = asyncio.Semaphore(CONCURRENCY)
        write_lock = asyncio.Lock()  # 写库串行: session 单线程访问
        dirty = 0                    # 已写但尚未 commit 的条数(攒批, 见 COMMIT_BATCH)

        async def _worker(kind, tid):
            nonlocal dirty
            async with sem:
                stats["n"] += 1
                item = await _fetch_one(cfg, kind, tid)
                if item is None:
                    stats["fail"] += 1
                else:
                    # 串行写库: 用同一把锁保证 session 不被并发访问
                    async with write_lock:
                        try:
                            # 记录旧行的图片 URL, 用于同步后清理"已换图"的旧缓存
                            old_obj = repo.get_tmdb_media_by_id(
                                session, item["kind"], item["tmdb_id"])
                            old_imgs = _image_urls_from_media(old_obj) if old_obj else set()
                            new_imgs = _image_urls_from_row(item["row"])
                            titles.apply_to_row(cfg, item["kind"], item["row"], old_obj,
                                                item.get("zh_fallback") or "")
                            repo.upsert_tmdb_media(session, item)
                            if item.get("seasons"):
                                repo.upsert_tmdb_seasons(session, item["tmdb_id"], item["seasons"])
                            stats["ok"] += 1
                            dirty += 1
                            if dirty >= COMMIT_BATCH:
                                # 攒 50 条一个事务(旧: 每条一个 commit —— 4444 条就是
                                # 4444 次事务开销; WAL 下单次不贵, 但纯属白开销)
                                session.commit()
                                dirty = 0
                            # 图片缓存联动: 旧图 URL 不再使用(换图) → 删旧缓存,
                            # 下次访问自动拉新图; 未变的 URL 不动(继续命中省带宽)
                            stale = old_imgs - new_imgs
                            if stale:
                                try:
                                    from server import imgproxy
                                    for u in stale:
                                        imgproxy.invalidate(u)
                                except Exception:  # noqa: BLE001
                                    pass
                        except Exception:  # noqa: BLE001
                            # 这条写了一半 → 回滚; 本批还没落盘的成功条目跟着一起回滚,
                            # 计数必须同步回退, 否则"成功 N"会比库里实际多。
                            if dirty:
                                stats["ok"] -= dirty
                                dirty = 0
                            session.rollback()
                            stats["fail"] += 1
                    if stats["n"] % 100 == 0 or stats["n"] >= total:
                        print(f"[tmdb] 进度 {stats['n']}/{total} "
                              f"(成功 {stats['ok']} 失败 {stats['fail']}) "
                              f"用时 {time.time()-t0:.0f}s", flush=True)

        await asyncio.gather(*[_worker(k, t) for k, t in todo])
        if dirty:                       # 收尾: 把最后一批落盘
            session.commit()
            dirty = 0
        print(f"[tmdb] 完成: 成功 {stats['ok']} / 失败 {stats['fail']} "
              f"用时 {time.time()-t0:.0f}s")
        return stats["ok"]
    finally:
        session.close()


def main():
    ap = argparse.ArgumentParser(description="同步 TMDB 元数据到本地 SQLite")
    ap.add_argument("--scope", default="all")
    ap.add_argument("--full", action="store_true", help="全量重刷(默认只刷缺失/过期条目)")
    ap.add_argument("--limit", type=int, default=0, help="只同步 N 条(调试)")
    ap.add_argument("--refresh-days", type=int, default=REFRESH_DAYS)
    args = ap.parse_args()
    try:
        from lib.loop_clients import run_coro
        n = run_coro(run(args.scope, full=args.full, limit=args.limit,
                         refresh_days=args.refresh_days))
        print(f"TMDB 同步完成, 共 {n} 条。")
    except Exception as e:  # noqa: BLE001
        print(f"TMDB 同步失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
