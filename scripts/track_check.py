#!/usr/bin/env python3
"""
media-auto / track_check —— 追踪检查引擎(演员新作 / 剧集新季 → 自动推磁力)
========================================================================

两种追踪:
  - person : TMDB person id。检测 person_credits 里【新增的电影】(对比基线)。
  - show   : TMDB tv id。检测 all_seasons 里【新增的季】(对比基线)。

"基线"(Track.baseline_items)的意义: 只追踪"开始追踪之后新出现的", 不把历史
作品/旧季全推一遍。首次添加时由路由写入基线(=当前已有), 之后每次 check 用
"当前 - 基线" 得新增。

自动推送(可全局开/关 + 每条覆盖):
  - 全局: settings 表 track_auto_push (默认关 —— 绝不偷偷往 115 推)。
  - 每条: Track.auto_push (None=跟随全局 / True=强制开 / False=强制关)。
  - 命中"分辨率 + 大小范围"筛选才推: 只认 4K 与 1080p, 各自有独立大小区间;
    其它分辨率一律不推。推不动(无命中)就只记录、不推 —— 宁可让用户手动判断,
    也不推一个可能不对的链接(与全项目"不兜底"一致)。

磁力搜索复用 server/routers/search.py 的 _pick_source / _fetch_all(双源:
原生 Bitmagnet GraphQL / Bitmagnet-Next-Web REST), 不另写一份(去重)。

入口:
  - 路由(异步上下文):  await check_async(cfg, log=...)
  - 调度器(线程):      run(cfg, log=...)  # 内部 asyncio.run
"""
import asyncio
import json
import re
from datetime import datetime

from db.database import SessionLocal
from db import repositories as repo
from db.models import Track

# ---- settings 表键(全局) ----
KEY_AUTO_PUSH = "track_auto_push"
KEY_4K_MIN = "track_size_4k_min_gb"
KEY_4K_MAX = "track_size_4k_max_gb"
KEY_1080_MIN = "track_size_1080_min_gb"
KEY_1080_MAX = "track_size_1080_max_gb"

_SEARCH_CAP = 60          # 单个标题最多拉多少条磁力参与筛选

# 4K 兜底识别: resolution_rank 只认 2160p/4320p 等像素写法, 但部分发布组直接写
# "4K"/"UHD"(无像素数)。mediainfo 自己的 quality_from_name 认 uhd, 这里对齐补齐。
_4K_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9])(?:4k|uhd|ultra[- .]?hd)(?![A-Za-z0-9])",
                          re.IGNORECASE)

# 全局检查锁: 防止手动触发与定时任务(或两次手动)并发 → 同批"新增"被推两遍。
# 用 threading.Lock 而非 asyncio.Lock: 调度器/手动检查各自 asyncio.run 新事件循环,
# 模块级 asyncio.Lock 跨循环会抛 "bound to a different loop"; 检查本就跑在线程里,
# 线程锁正合适。
import threading
_check_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 设置读取
# ---------------------------------------------------------------------------
def _f(raw):
    """把 settings 字符串安全转 float; 空/非法 → None(=不限)。"""
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_track_settings(session):
    """全局追踪设置: 自动推送开关 + 4K/1080p 各自大小区间(GB)。"""
    return {
        "auto_push": repo.get_setting(session, KEY_AUTO_PUSH, "false") == "true",
        "size_4k": (_f(repo.get_setting(session, KEY_4K_MIN, "")),
                    _f(repo.get_setting(session, KEY_4K_MAX, ""))),
        "size_1080": (_f(repo.get_setting(session, KEY_1080_MIN, "")),
                      _f(repo.get_setting(session, KEY_1080_MAX, ""))),
    }


def _effective_auto_push(track, settings):
    """该条是否自动推: 覆盖位优先, 否则跟随全局。"""
    if track.auto_push is None:
        return settings["auto_push"]
    return bool(track.auto_push)


# ---------------------------------------------------------------------------
# 分辨率 / 大小筛选
# ---------------------------------------------------------------------------
def _resolution_class(name):
    """文件名 → '4k' | '1080p' | None(只认这两档, 其它不推)。

    像素写法(2160p/4320p/1080p)走 mediainfo.resolution_rank;
    裸 4K/UHD 写法走 _4K_ALIAS_RE 兜底(见上, 与 quality_from_name 的 uhd 对齐)。
    """
    from lib import mediainfo
    w = mediainfo.resolution_rank(name or "")
    if w in (4, 5):      # 2160p / 4320p
        return "4k"
    if w == 0 and _4K_ALIAS_RE.search(name or ""):
        return "4k"
    if w == 3:           # 1080p
        return "1080p"
    return None


def _passes(size_bytes, name, settings):
    """大小(字节)+ 分辨率是否落在该档的区间内。None 边界 = 不限。"""
    cls = _resolution_class(name)
    if cls is None:
        return False
    lo, hi = settings["size_4k"] if cls == "4k" else settings["size_1080"]
    try:
        gb = float(size_bytes) / (1024 ** 3)
    except (TypeError, ValueError):
        return False
    if lo is not None and gb < lo:
        return False
    if hi is not None and gb > hi:
        return False
    return True


def _pick_best(items):
    """已过筛选的磁力里挑最优: 种子数优先(无则 0), 再按体积大者优先。"""
    if not items:
        return None
    return sorted(items, key=lambda x: (x.get("seeders") or 0, x.get("size") or 0),
                  reverse=True)[0]


# ---------------------------------------------------------------------------
# 磁力搜索(复用 router/search 的双源实现)
# ---------------------------------------------------------------------------
async def _search_magnets(cfg, title_en, title_cn, cap=_SEARCH_CAP):
    """按标题搜磁力(英文优先, 中文兜底, 按 infoHash 去重合并)。"""
    from server.routers import search as srouter
    source = srouter._pick_source(cfg)
    if source is None:
        return []
    seen, out = set(), []
    for kw in (title_en, title_cn):
        if not kw or not kw.strip():
            continue
        try:
            got = await srouter._fetch_all(source, cfg, kw.strip(), cap)
        except Exception:  # noqa: BLE001  单源失败不致命
            continue
        for it in got:
            h = it.get("infoHash")
            if not h or h in seen:
                continue
            seen.add(h)
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# 新增检测
# ---------------------------------------------------------------------------
def _load_baseline(track):
    try:
        return set(json.loads(track.baseline_items or "[]"))
    except (TypeError, ValueError):
        return set()


def _save_baseline(track, items):
    track.baseline_items = json.dumps(sorted(items), ensure_ascii=False)


async def _detect_person_new(cfg, track):
    """演员新作: person_credits 的 movies 里不在基线内的。

    返回 (new_cards, all_ids, first_run)。⚠️ first_run(基线为空)= 只初始化基线,
    **不**把历史作品当"新作" —— 否则一追踪某演员就把他整个片库全推了。
    """
    from clients.tmdb import client as tmdb
    credits = await tmdb.person_credits(cfg, track.ref_id, limit_per_kind=200)
    movies = [c for c in (credits.get("movies") or []) if c.get("tmdbId")]
    baseline = _load_baseline(track)
    first_run = not baseline
    new = [] if first_run else [c for c in movies if c["tmdbId"] not in baseline]
    return new, [c["tmdbId"] for c in movies], first_run


async def _detect_show_new(cfg, track):
    """剧集新季: all_seasons 里不在基线内的季。

    返回 (new_seasons, all_numbers, first_run)。first_run 时只初始化基线,
    旧季不算"新季"(不把整部剧的历史季全推)。
    """
    from clients.tmdb import client as tmdb
    seasons = await tmdb.all_seasons(cfg, track.ref_id)
    seasons = [s for s in seasons if isinstance(s.get("number"), int)]
    baseline = _load_baseline(track)
    first_run = not baseline
    new = [] if first_run else [s for s in seasons if s["number"] not in baseline]
    return new, [s["number"] for s in seasons], first_run


# ---------------------------------------------------------------------------
# 单个新增作品: 搜磁力 → 筛选 → (可选)推送
# ---------------------------------------------------------------------------
async def _handle_items(cfg, settings, to_folder, base_dir, push_enabled,
                        kind, title, items, log):
    """对已搜到的磁力列表: 筛选(分辨率+大小)→ 挑最优 → (可选)推 CD2。

    返回 (pushed: bool, note: str)。pushed=True 表示已推 CD2。
    """
    if not items:
        return False, "无磁力"
    hit = _pick_best([it for it in items if _passes(it.get("size"), it.get("name"), settings)])
    if not hit:
        return False, f"{len(items)} 条磁力, 无符合分辨率/大小的"
    if not push_enabled:
        # 干跑: 只记录会推什么, 不真推
        gb = (hit.get("size") or 0) / (1024 ** 3)
        return False, (f"干跑: 将推 {hit.get('name', '')[:40]} "
                       f"({_resolution_class(hit.get('name'))}, {gb:.1f}GB)")
    from clients.clouddrive import client as cd2
    from lib import state as state_mod
    from server.config import CONFIG_PATH
    try:
        await asyncio.to_thread(cd2.add_offline, cfg, hit.get("magnet"), to_folder, base_dir)
    except Exception as e:  # noqa: BLE001
        return False, f"推 CD2 失败: {str(e)[:120]}"
    # 记入下载队列(与手动推送同一去重)
    try:
        from scripts.push import build_task
        task = build_task(hit.get("magnet"), {"title": title, "content_type": kind},
                          to_folder)
        state_mod.add_task(CONFIG_PATH, task)
    except Exception as e:  # noqa: BLE001
        log(f"    ⚠ 记队列失败(链接已推): {e}")
    return True, f"已推 {hit.get('name', '')[:40]}"


async def _handle_new(cfg, settings, to_folder, base_dir, push_enabled,
                      kind, title, title_en, log):
    """搜磁力 + 筛选 + 推送(电影分支用, 每部各搜一次)。"""
    items = await _search_magnets(cfg, title_en or title, title or "", _SEARCH_CAP)
    return await _handle_items(cfg, settings, to_folder, base_dir, push_enabled,
                               kind, title, items, log)


# ---------------------------------------------------------------------------
# 单条 track 检查
# ---------------------------------------------------------------------------
async def _check_one(cfg, settings, track, to_folder, base_dir, log):
    push_enabled = _effective_auto_push(track, settings)

    if track.kind == "person":
        new_items, all_ids, first_run = await _detect_person_new(cfg, track)
        label = "新作"
        detail_list = []
        pushed = 0
        for c in new_items[:20]:  # 上限 20, 防极端情况刷屏
            title = c.get("title") or ""
            year = str(c.get("year") or "")
            title_en = ""
            try:
                from clients.tmdb import client as tmdb
                title_en = await tmdb.english_title(cfg, "movie", c["tmdbId"])
            except Exception:  # noqa: BLE001
                pass
            ok, note = await _handle_new(cfg, settings, to_folder, base_dir, push_enabled,
                                         "movie", title, title_en, log)
            if ok:
                pushed += 1
            detail_list.append(f"{title}({year or '?'}): {note}")
    else:  # show
        new_seasons, all_numbers, first_run = await _detect_show_new(cfg, track)
        label = "新季"
        detail_list = []
        pushed = 0
        if new_seasons:
            # 磁力通常是整剧/整季包, 按剧名搜一次即可, 不逐季重复推同一条。
            # 用英文剧名搜更准(中文剧名磁力多为字幕组名)。
            title = track.name
            title_en = ""
            try:
                from clients.tmdb import client as tmdb
                title_en = await tmdb.english_title(cfg, "tv", track.ref_id)
            except Exception:  # noqa: BLE001
                pass
            items = await _search_magnets(cfg, title_en or title, title or "", _SEARCH_CAP)
            ok, note = await _handle_items(cfg, settings, to_folder, base_dir, push_enabled,
                                           "tv", title, items, log)
            if ok:
                pushed += 1
            nums = ", ".join(f"S{s['number']:02d}" for s in new_seasons[:10])
            detail_list.append(f"{nums}: {note}")

    # 更新基线(含"首次初始化" —— 首跑把当前全集写入, 下次才有 diff)
    baseline_items = all_ids if track.kind == "person" else all_numbers
    _save_baseline(track, baseline_items)
    track.last_checked_at = datetime.now()
    n_new = len(new_items) if track.kind == "person" else len(new_seasons)
    if first_run:
        summary = f"首次: 建基线 {len(baseline_items)} 条, 之后才检测新增"
    else:
        summary = (f"{label} {n_new}" + (f", 推 {pushed}" if push_enabled else ", 干跑"))
    track.last_result = (summary + (" | " + "; ".join(detail_list[:5])
                                    if detail_list else ""))[:500]
    log(f"  [{track.kind}:{track.name}] {summary}")
    return {"kind": track.kind, "name": track.name, "new": n_new,
            "pushed": pushed, "first": first_run, "details": detail_list}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def check_async(cfg, log=print):
    """检查全部追踪项。返回 {total, checked, new_total, pushed, details:[...]}。"""
    from server.config import skill_root
    # 并发锁: 手动触发与 6h 定时可能撞车, 后到的直接跳过(基线已更新, 没有可推的)
    if not _check_lock.acquire(blocking=False):
        log("追踪检查: 已有检查在跑, 本次跳过")
        return {"total": 0, "checked": 0, "new_total": 0, "pushed": 0, "details": []}
    try:
        s = SessionLocal()
        results = []
        try:
            settings = get_track_settings(s)
            # 落盘目录走 organize.offline_root 唯一来源(与整理/CD2 路由同一读法),
            # 不自己再写一份 offline_root 兜底(避免两处默认值漂移)。
            from scripts import organize
            to_folder = organize.offline_root(cfg)
            base_dir = skill_root()
            tracks = s.query(Track).all()
            log(f"追踪检查: 共 {len(tracks)} 条 (全局自动推送={'开' if settings['auto_push'] else '关'})")
            for t in tracks:
                try:
                    r = await _check_one(cfg, settings, t, to_folder, base_dir, log)
                    results.append(r)
                except Exception as e:  # noqa: BLE001  单条失败不影响其它
                    log(f"  ⚠ [{t.kind}:{t.name}] 检查失败: {str(e)[:160]}")
                    t.last_checked_at = datetime.now()
                    t.last_result = f"检查失败: {str(e)[:200]}"
                    results.append({"kind": t.kind, "name": t.name, "new": 0,
                                    "pushed": 0, "first": False, "details": [], "error": str(e)[:200]})
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
        return {
            "total": len(results),
            "checked": len(results),
            "new_total": sum(r.get("new", 0) for r in results),
            "pushed": sum(r.get("pushed", 0) for r in results),
            "details": results,
        }
    finally:
        _check_lock.release()


def run(cfg=None, log=print):
    """同步入口(供调度器线程调用)。cfg 缺省则现读 config.json。"""
    if cfg is None:
        from server.config import get_config
        cfg = get_config()
    return asyncio.run(check_async(cfg, log=log))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="手动跑一次追踪检查")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    from server.config import get_config
    out = run(get_config(), log=(print if args.verbose else (lambda *a, **k: None)))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
