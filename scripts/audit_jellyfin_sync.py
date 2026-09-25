#!/usr/bin/env python3
"""media-auto / audit_jellyfin_sync —— Jellyfin → 本地库 同步完整性审计(只读)

================================================================
用途: 用户问"增量/全量同步是否完整, 电影和剧集的分集是否都同步过来了, 有没有问题"
时跑一次, 用**真实 Jellyfin 数据**逐项交叉验证, 而不是只看代码。

只读: 只查 Jellyfin + 读本地 SQLite, 不写任何数据。

审计项:
  [1] A 层条目镜像    Jellyfin 全库(Movie/Series) vs jellyfin_item: 缺行/陈旧行
  [2] 分集完整性      Jellyfin 全量 Episode vs jf_episode: 逐系列缺集/多集  ← 核心
  [3] 可用性完整性    每个在库作品是否有 media 行且状态合理(不落 UNKNOWN)
  [4] 最近新增分集    Jellyfin 最近 N 天新增的集, 本地是否都已同步  ← 核心
  [5] 引用一致性      jellyfin_media_id 悬空 / 分集孤儿 / 重复行 / season 悬挂
  [6] 写入模型核验    全项目不得再出现"清空本地行 / 快照闸门"类接口  ← 设计符合性
  [7] 删除权限收敛    只有 availability_sync 能产生 DELETED         ← 设计符合性
  [8] 窗口式增量模拟  recent 窗口必须取自全库最新的那一段(且来自在库全量)

用法:
  python3 scripts/audit_jellyfin_sync.py
  python3 scripts/audit_jellyfin_sync.py --recent-days 14 --sample 60
"""
import argparse
import asyncio
import io
import os
import re
import sys
import tokenize
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import load_config
from clients.jellyfin import client as jf
from db.database import SessionLocal, init_db
from db.models import JellyfinItem, JfEpisode, Media, MediaStatus, MediaType, Season
from db import repositories as repo
from sqlalchemy import func

OK, BAD, WARN = "\033[32m✅\033[0m", "\033[31m❌\033[0m", "\033[33m⚠️\033[0m"
_fails = []


def check(cond, msg, warn_only=False):
    tag = OK if cond else (WARN if warn_only else BAD)
    print(f"  {tag} {msg}")
    if not cond and not warn_only:
        _fails.append(msg)
    return cond


async def _walk(cfg, item_types, fields, pages=None, sort="DateCreated"):
    """独立翻页(仅用于构造基准, 与被审计实现解耦)。"""
    out, start = [], 0
    while True:
        if pages is not None and start // jf._PAGE_SIZE >= pages:
            break
        d = await jf._jf_get(cfg, "/Items", {
            "Recursive": "true", "Fields": fields, "IncludeItemTypes": item_types,
            "SortBy": sort, "SortOrder": "Descending",
            "StartIndex": start, "Limit": jf._PAGE_SIZE})
        rs = (d or {}).get("Items") or []
        if not rs:
            break
        out.extend(rs)
        start += len(rs)
        if start >= ((d or {}).get("TotalRecordCount") or 0):
            break
    return out


async def main(cfg, recent_days, sample):
    init_db()
    s = SessionLocal()

    # ============================================================ [1] A 层
    print("\n[1] A 层条目镜像 —— Jellyfin 全库 vs jellyfin_item")
    live = await jf.list_items(cfg)                      # 被审计函数
    live_ids = {it["item_id"] for it in live if it["item_id"]}
    live_by_id = {it["item_id"]: it for it in live}
    a_rows = {r.item_id: r for r in s.query(JellyfinItem).all()}
    print(f"  Jellyfin: Movie+Series {len(live)} 个 | 本地 jellyfin_item {len(a_rows)} 个")
    missing = live_ids - set(a_rows)                     # Jellyfin 有本地无(致命)
    stale = set(a_rows) - live_ids                       # 本地有 Jellyfin 无(陈旧)
    check(not missing, f"Jellyfin 有、本地缺的条目应为 0, 实际 {len(missing)}"
                       + (f" 例: {sorted(missing)[:5]}" if missing else ""))
    check(not stale, f"本地多出(Jellyfin 已无)应为 0, 实际 {len(stale)}"
                     + (f" 例: {[(i, a_rows[i].name) for i in sorted(stale)[:5]]}"
                        if stale else ""), warn_only=True)

    # 关键字段一致性(全量, 逐一比)
    bad_field = []
    for iid, it in live_by_id.items():
        r = a_rows.get(iid)
        if r is None:
            continue
        for k, v in (("name", it["name"]), ("type", it["type"]),
                     ("tmdb_id", it["tmdb_id"]), ("year", it["year"])):
            if (getattr(r, k) or "") != (v or "") and getattr(r, k) != v:
                bad_field.append((iid, r.name, k, getattr(r, k), v))
                break
    check(not bad_field, f"条目字段不一致应为 0, 实际 {len(bad_field)}"
                         + (f" 例: {bad_field[:3]}" if bad_field else ""))

    # ============================================================ [2] 分集完整性
    print("\n[2] 分集完整性 —— Jellyfin 全量 Episode vs jf_episode  ← 核心")
    jf_eps = await jf.list_episodes(cfg)                 # 被审计函数
    jf_by_series = defaultdict(set)
    for e in jf_eps:
        if e["series_id"]:
            jf_by_series[e["series_id"]].add((e["season"], e["episode"]))
    local = defaultdict(set)
    for sid, se, ep in s.query(JfEpisode.series_id, JfEpisode.season, JfEpisode.episode):
        local[sid].add((se, ep))
    jf_keys = sum(len(v) for v in jf_by_series.values())
    local_keys = sum(len(v) for v in local.values())
    total_rows = s.query(JfEpisode).count()
    print(f"  Jellyfin 分集 {len(jf_eps)} 条 / 去重 {jf_keys} 个季集号 / {len(jf_by_series)} 部剧")
    print(f"  本地 jf_episode {total_rows} 行 / 去重 {local_keys} 个季集号 / {len(local)} 部剧")
    print("  说明: 行数 > 季集号数, 是 Jellyfin 把无编号的番外/特典都报成 E00 造成的")
    print("        「同(剧,季,集)多行」(如 S01E00 重复上百次), 属既有现象, 非数据丢失。")
    diff_missing, diff_extra = [], []
    for sid, want in jf_by_series.items():
        have = local.get(sid, set())
        if want - have:
            diff_missing.append((sid, a_rows[sid].name if sid in a_rows else sid,
                                 len(want - have), sorted(want - have)[:4]))
        if have - want:
            diff_extra.append((sid, a_rows[sid].name if sid in a_rows else sid,
                               len(have - want), sorted(have - want)[:4]))
    check(not diff_missing,
          f"本地缺分集的剧应为 0, 实际 {len(diff_missing)} 部"
          + "".join(f"\n       · {n}({sid[:8]}…) 缺 {c} 集 例{s}" for sid, n, c, s
                    in diff_missing[:10]))
    check(not diff_extra,
          f"本地多出分集的剧应为 0, 实际 {len(diff_extra)} 部"
          + "".join(f"\n       · {n}({sid[:8]}…) 多 {c} 集 例{s}" for sid, n, c, s
                    in diff_extra[:10]))
    # 反向: 本地有分集但剧不在 Jellyfin 列表里(陈旧)
    orphan_series = {sid for sid in local if sid not in live_ids}
    check(not orphan_series, f"分集挂在已不在库的剧上应为 0, 实际 {len(orphan_series)}"
                             + (f" 例: {sorted(orphan_series)[:5]}" if orphan_series else ""),
          warn_only=True)

    # ============================================================ [3] 可用性完整性
    print("\n[3] 可用性完整性 —— 在库作品是否都有 media 行且状态合理")
    med = {(m.tmdb_id, m.media_type): m for m in s.query(Media).all()}
    no_row, bad_status = [], []
    for it in live:
        if not it["tmdb_id"].isdigit():
            continue
        mt = MediaType.MOVIE if it["type"] == "Movie" else MediaType.TV
        m = med.get((int(it["tmdb_id"]), mt))
        if m is None:
            no_row.append((it["name"], it["type"], it["tmdb_id"]))
        elif mt == MediaType.TV and m.status in (MediaStatus.UNKNOWN, MediaStatus.PROCESSING):
            bad_status.append((it["name"], m.status,
                               len(local.get(it["item_id"], set()))))
    check(not no_row, f"在库作品缺 media 行应为 0, 实际 {len(no_row)}"
                      + "".join(f"\n       · {n}({t}, tmdb={i})" for n, t, i in no_row[:10]))
    check(not bad_status,
          f"在库剧集落 UNKNOWN/PROCESSING 应为 0, 实际 {len(bad_status)}"
          + "".join(f"\n       · {n} status={st} 本地实有集={c}" for n, st, c in bad_status[:10]),
          warn_only=True)
    movies_live = sum(1 for it in live if it["type"] == "Movie")
    movies_ok = sum(1 for it in live if it["type"] == "Movie"
                    and it["tmdb_id"].isdigit()
                    and med.get((int(it["tmdb_id"]), MediaType.MOVIE)) is not None)
    print(f"  电影: 在库 {movies_live} 部, 有 media 行 {movies_ok} 部")

    # ============================================================ [4] 最近新增分集
    print(f"\n[4] 最近 {recent_days} 天新增的分集, 本地是否都已同步  ← 核心")
    # 用本地 now() 作为下界: Jellyfin 的 DateCreated 实测是"服务器本地时间(Z 结尾)"，
    # 与本地墙钟同尺度, 不要用 datetime.now(timezone.utc)(会差 8 小时)。
    floor = datetime.now() - timedelta(days=recent_days)
    # 只翻到窗口边界为止(便宜)
    recent_eps, start = [], 0
    while True:
        d = await jf._jf_get(cfg, "/Items", {
            "Recursive": "true", "Fields": "SeriesId,IndexNumber,ParentIndexNumber,DateCreated",
            "IncludeItemTypes": "Episode", "SortBy": "DateCreated", "SortOrder": "Descending",
            "StartIndex": start, "Limit": jf._PAGE_SIZE})
        rs = (d or {}).get("Items") or []
        if not rs:
            break
        stop = False
        for it in rs:
            dt = jf.parse_jf_time(it.get("DateCreated"))
            if dt is not None and dt.replace(tzinfo=None) < floor:
                stop = True
                break
            recent_eps.append(it)
        if stop:
            break
        start += len(rs)
        if start >= ((d or {}).get("TotalRecordCount") or 0):
            break
    by_s = defaultdict(list)
    for it in recent_eps:
        sid = it.get("SeriesId")
        if sid:
            by_s[sid].append((int(it.get("ParentIndexNumber") or 0),
                              int(it.get("IndexNumber") or 0)))
    not_synced = []
    for sid, eps in by_s.items():
        have = local.get(sid, set())
        miss = [e for e in eps if e not in have]
        if miss:
            not_synced.append((a_rows[sid].name if sid in a_rows else sid, len(miss), miss[:5]))
    print(f"  窗口内新增分集 {len(recent_eps)} 条, 涉及 {len(by_s)} 部剧")
    check(not not_synced,
          f"窗口内新增分集本地未同步的剧应为 0, 实际 {len(not_synced)}"
          + "".join(f"\n       · {n} 缺 {c} 集 例{s}" for n, c, s in not_synced[:10]))

    # ============================================================ [5] 引用一致性
    print("\n[5] 引用一致性")
    dangling = [m for m in med.values()
                if m.jellyfin_media_id and m.jellyfin_media_id not in live_ids]
    check(not dangling, f"media.jellyfin_media_id 悬空应为 0, 实际 {len(dangling)}"
                        + "".join(f"\n       · {m.title} → {m.jellyfin_media_id[:12]}…"
                                  for m in dangling[:8]))
    dup = (s.query(Media.tmdb_id, Media.media_type, func.count())
           .group_by(Media.tmdb_id, Media.media_type).having(func.count() > 1).all())
    check(not dup, f"重复 media 行应为 0, 实际 {len(dup)}")
    no_tmdb = s.query(Media).filter((Media.tmdb_id.is_(None)) | (Media.tmdb_id == 0)).count()
    check(no_tmdb == 0, f"tmdb_id 为空的 media 行应为 0, 实际 {no_tmdb}", warn_only=True)
    orphan_season = (s.query(Season).outerjoin(Media, Season.media_id == Media.id)
                     .filter(Media.id.is_(None)).count())
    check(orphan_season == 0, f"悬挂 season 行应为 0, 实际 {orphan_season}")

    # ============================================================ [6] 写入模型核验
    # 2026-09-22 照 Seerr 重做后, 全项目**不得**再出现"清空本地行"或"快照完整性闸门"
    # 这类接口 —— 它们是旧"先清空再重建"模型的产物, 也正是"完整数据变缺失"的根源。
    # 这里做静态核验, 防止有人在后续迭代里把它们加回来。
    print("\n[6] 写入模型核验 —— 全项目不得存在清空/闸门类接口")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scanned = ("scripts/sync_jellyfin.py", "scripts/sync_jf_scanner.py",
               "scripts/availability_sync.py", "db/repositories.py",
               "clients/jellyfin/client.py")
    forbidden = ("wipe_jellyfin_items", "wipe_jf_episodes", "count_jf_episodes",
                 "snapshot_complete", "is_library_scan_running",
                 "list_items_incremental", "list_episode_series_incremental",
                 "next_cursor", "now_like_jf")
    offenders = []
    for fn in scanned:
        path = os.path.join(root, fn)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        # 用 tokenize 取"真实标识符", 自动忽略注释与字符串 ——
        # 否则注释里写的"这里原本有 wipe_xxx 已被删除"会被误报为"接口还在"。
        idents = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.NAME:
                    idents.add(tok.string)
        except (tokenize.TokenError, IndentationError):
            pass
        for name in forbidden:
            if name in idents:
                offenders.append(f"{fn}: {name}")
    check(not offenders,
          "不应再出现清空/闸门类接口(已被 纯 upsert + fail-safe 单查 取代): "
          + ", ".join(offenders))

    # ============================================================ [7] 删除权限收敛
    print("\n[7] 删除权限收敛 —— 只有 availability_sync 能产生 DELETED")
    del_offenders = []
    for fn in ("scripts/sync_jellyfin.py", "scripts/sync_jf_scanner.py",
               "clients/jellyfin/client.py", "db/repositories.py"):
        path = os.path.join(root, fn)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        # 只找赋值的 '=', 排除 '==' 之类的比较(用否定后顾)
        for m in re.finditer(r"(?<![=!<>])=\s*MediaStatus\.DELETED", src):
            del_offenders.append(f"{fn}:{src[:m.start()].count(chr(10)) + 1}")
    check(not del_offenders,
          "DELETED 只应由 availability_sync 写入, 其它处出现赋值: "
          + ", ".join(del_offenders))

    # ============================================================ [8] 窗口式增量模拟
    print("\n[8] 窗口式增量模拟 —— recent 窗口取自全库最新那一段")
    win = await jf.list_recent_items(cfg)          # 被审计函数
    win_ids = {i["item_id"] for i in win}
    check(win_ids <= live_ids,
          f"窗口内条目应都来自在库全量(越界 {len(win_ids - live_ids)} 条)")
    newest_live = max((it["date_created"] for it in live if it["date_created"]),
                      default=None)
    newest_win = max((it["date_created"] for it in win if it["date_created"]),
                     default=None)
    check(newest_win is None or newest_win == newest_live,
          f"窗口必须覆盖全库最新条目(库最新 {newest_live} / 窗口最新 {newest_win})")
    ep_sids = await jf.list_recent_episode_series(cfg)
    print(f"  条目窗口 {len(win)} 条 | 分集窗口涉及 {len(ep_sids)} 部剧")

    s.close()
    print()
    if _fails:
        print(f"{BAD} 审计发现 {len(_fails)} 项问题")
        for m in _fails:
            print(f"   - {m.splitlines()[0]}")
        return 1
    print(f"{OK} 审计通过: 同步链路完整, 无问题")
    return 0


def cli():
    ap = argparse.ArgumentParser(description="Jellyfin→本地库 同步完整性审计(只读)")
    ap.add_argument("--recent-days", type=int, default=7)
    ap.add_argument("--sample", type=int, default=60)
    a = ap.parse_args()
    cfg = load_config()
    if not cfg.get("jellyfin", {}).get("url"):
        print("config.json 未配置 jellyfin.url", file=sys.stderr)
        return 2
    return asyncio.run(main(cfg, a.recent_days, a.sample))


if __name__ == "__main__":
    sys.exit(cli())
