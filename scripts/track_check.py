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
  - 命中「推送筛选 + 大小范围」才推, 四个维度全过才推:
      分辨率: track_resolutions, 可选 2160p/1080p/720p/480p;
              **从未设置过 = 默认 2160p,1080p**(= 上线前的历史行为), 显式清空 = 全部档;
      片源:   track_sources, 可选 REMUX/BLURAY/WEBDL/WEBRIP/HDTV/DVD; 空 = 不限;
      发布组: track_groups, 名字尾巴 -GROUP 匹配(如 SPARK,Sai); 空 = 不限;
      大小:   2160p/1080p 各自独立区间; 其它分辨率没有区间 = 不限大小。
    **认不出就不推**: 分辨率认不出 → 不推; 开了片源/发布组筛选而名字里认不出 → 不推
    (不猜, 宁可让用户手动判断, 也不推一个可能不对的链接)。
    推不动(无命中)就只记录、不推。

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
KEY_RESOLUTIONS = "track_resolutions"   # 允许的分辨率(逗号分隔); 从未设置 = 默认两档
KEY_SOURCES = "track_sources"           # 允许的片源(REMUX/BLURAY/WEBDL/WEBRIP/HDTV/DVD); 空 = 不限
KEY_GROUPS = "track_groups"             # 允许的发布组(如 SPARK,Sai); 空 = 不限

_SEARCH_CAP = 60          # 单个标题最多拉多少条磁力参与筛选

# 分辨率档位(推送筛选的可选项)。大小区间只有 2160p/1080p 两档, 其余不限大小。
RESOLUTION_OPTIONS = ("2160p", "1080p", "720p", "480p")
DEFAULT_RESOLUTIONS = ("2160p", "1080p")   # 从未设置过 → 保持历史行为: 只推这两档
# 片源选项(值 = _source_of 的返回, 与 mediainfo.guess_source 同口径 + WEBRIP 细分)
SOURCE_OPTIONS = ("REMUX", "BLURAY", "WEBDL", "WEBRIP", "HDTV", "DVD")

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


def _csv(raw):
    """settings 字符串 → 去空去重保序的列表(逗号分隔)。"""
    out, seen = [], set()
    for x in str(raw or "").split(","):
        v = x.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def get_track_settings(session):
    """全局追踪设置: 自动推送开关 + 大小区间 + 片源/分辨率/发布组筛选。"""
    size_4k = (_f(repo.get_setting(session, KEY_4K_MIN, "")),
               _f(repo.get_setting(session, KEY_4K_MAX, "")))
    size_1080 = (_f(repo.get_setting(session, KEY_1080_MIN, "")),
                 _f(repo.get_setting(session, KEY_1080_MAX, "")))
    # 分辨率: 从未设置过 → 默认两档(= 本功能上线前的历史行为); 显式清空 → 不限
    raw_res = repo.get_setting(session, KEY_RESOLUTIONS, "__default__")
    resolutions = list(DEFAULT_RESOLUTIONS) if raw_res == "__default__" else _csv(raw_res)
    return {
        "auto_push": repo.get_setting(session, KEY_AUTO_PUSH, "false") == "true",
        "size_4k": size_4k,
        "size_1080": size_1080,
        "resolutions": resolutions,
        "sources": _csv(repo.get_setting(session, KEY_SOURCES, "")),
        "groups": _csv(repo.get_setting(session, KEY_GROUPS, "")),
        # 大小区间按分辨率档取(720p/480p 没有档 → 不限)
        "size": {"2160p": size_4k, "1080p": size_1080},
    }


# 常见写法别名 → 规范值(用户手输/旧口径进来也能归一, 未列出的一律丢弃)
_RES_ALIAS = {"4K": "2160p", "UHD": "2160p", "4320P": "2160p",
              "1080": "1080p", "1080I": "1080p", "720": "720p", "480": "480p",
              "480I": "480p", "576P": "480p"}
_SOURCE_ALIAS = {"WEB-DL": "WEBDL", "WEBDL": "WEBDL", "WEB": "WEBDL",
                 "WEB-RIP": "WEBRIP", "WEBRIP": "WEBRIP",
                 "BLU-RAY": "BLURAY", "BLURAY": "BLURAY", "BD": "BLURAY",
                 "BDRIP": "BLURAY", "REMUX": "REMUX", "HDTV": "HDTV", "DVD": "DVD"}


def normalize_setting_lists(resolutions=None, sources=None, groups=None):
    """把前端传来的三组筛选规整成可入库的逗号串(未知项丢弃, 组名去重保序)。

    传 None = 不改该项; 传 "" = 清成不限。
    """
    def _norm(vals, allowed, alias):
        if vals is None:
            return None
        canon = {a.upper(): a for a in allowed}
        keep = []
        for v in _csv(vals):
            key = canon.get(v.upper()) or canon.get((alias.get(v.upper()) or "").upper())
            if key and key not in keep:
                keep.append(key)
        return ",".join(keep)
    return {
        "resolutions": _norm(resolutions, RESOLUTION_OPTIONS, _RES_ALIAS),
        "sources": _norm(sources, SOURCE_OPTIONS, _SOURCE_ALIAS),
        "groups": ",".join(_csv(groups)) if groups is not None else None,
    }


def _rule_summary(settings):
    """当前筛选的可读摘要(检查日志/无命中提示用)。"""
    parts = []
    if settings.get("resolutions"):
        parts.append("/".join(settings["resolutions"]))
    if settings.get("sources"):
        parts.append("+".join(settings["sources"]))
    if settings.get("groups"):
        parts.append("组 " + "/".join(settings["groups"]))
    for label, key in (("2160p", "2160p"), ("1080p", "1080p")):
        lo, hi = settings.get("size", {}).get(key, (None, None))
        if lo is not None or hi is not None:
            parts.append(f"{label} {lo if lo is not None else 0}–{hi if hi is not None else '∞'}GB")
    return " · ".join(parts) if parts else "全部分辨率(认不出仍不推)"


def _effective_auto_push(track, settings):
    """该条是否自动推: 覆盖位优先, 否则跟随全局。"""
    if track.auto_push is None:
        return settings["auto_push"]
    return bool(track.auto_push)


# ---------------------------------------------------------------------------
# 分辨率 / 片源 / 发布组 / 大小筛选
# ---------------------------------------------------------------------------
def _resolution_class(name):
    """文件名 → '2160p' | '1080p' | '720p' | '480p' | None(认不出 → 一律不推)。

    像素写法(2160p/4320p/1080p/720p/576p…)走 mediainfo.resolution_rank;
    裸 4K/UHD 写法走 _4K_ALIAS_RE 兜底(见上, 与 quality_from_name 的 uhd 对齐)。
    """
    from lib import mediainfo
    w = mediainfo.resolution_rank(name or "")
    if w in (4, 5):      # 2160p / 4320p
        return "2160p"
    if w == 0 and _4K_ALIAS_RE.search(name or ""):
        return "2160p"
    return {3: "1080p", 2: "720p", 1: "480p"}.get(w)


# WEBRIP 要与 WEB-DL 分开(用户按片源筛), 而 mediainfo.guess_source 把 webrip 并进
# WEBDL(NFO <source> 口径) —— 这里先单独判 WEBRIP, 其余复用 guess_source 单一来源。
_WEBRIP_RE = re.compile(r"(?<![0-9A-Za-z_])web[-. ]?rip(?![0-9A-Za-z_])", re.IGNORECASE)
# 磁力标题里可能带扩展名, 尾巴解析发布组前先剥掉
_EXT_RE = re.compile(r"\.(mkv|mp4|avi|ts|m2ts|wmv|mov|iso|rmvb)$", re.IGNORECASE)


def _source_of(name):
    """片源细分: REMUX | BLURAY | WEBDL | WEBRIP | HDTV | DVD | NONE(认不出)。"""
    from lib import mediainfo
    s = str(name or "")
    if _WEBRIP_RE.search(s):
        return "WEBRIP"
    return mediainfo.guess_source(s)


def _group_of(name):
    """发布组(名字尾巴 -GROUP): 认不出返回 ''。

    与 naming.RELEASE_GROUP 同一口径(去掉发布组后缀就是用它), 剥掉扩展名再匹配。
    """
    from lib.naming import RELEASE_GROUP
    n = _EXT_RE.sub("", str(name or "").strip())
    m = RELEASE_GROUP.search(n)
    return m.group(0)[1:] if m else ""


def _passes(size_bytes, name, settings):
    """片源 + 分辨率 + 发布组 + 大小 —— 全部命中才推。

    「认不出就不推」: 分辨率认不出 → 不推; 开了片源/发布组筛选而名字里认不出 → 不推
    (与全项目"不兜底"一致, 宁可让用户手动判断)。边界值 None = 不限;
    大小区间只有 2160p/1080p 两档, 其它分辨率没有区间 = 不限大小。
    """
    cls = _resolution_class(name)
    if cls is None:
        return False
    if settings.get("resolutions") and cls not in settings["resolutions"]:
        return False
    if settings.get("sources"):
        got = _source_of(name)
        if got == "NONE" or got not in settings["sources"]:
            return False
    if settings.get("groups"):
        got = _group_of(name)
        if not got or got.lower() not in {g.lower() for g in settings["groups"]}:
            return False
    lo, hi = settings.get("size", {}).get(cls, (None, None))
    try:
        gb = float(size_bytes) / (1024 ** 3)
    except (TypeError, ValueError):
        return False
    if lo is not None and gb < lo:
        return False
    if hi is not None and gb > hi:
        return False
    return True


def _pick_best(items, cfg=None):
    """已过筛选的磁力里挑最优: 前排组(种子抓取规则)优先, 再种子数, 再体积大者。

    前排组规则与详情页搜索共用 `server.routers.search.group_priority_list`(顺序 = 优先级),
    所以详情页怎么排, 追踪自动推送就怎么挑; 配置为空数组 = 回到纯种子数口径。
    """
    if not items:
        return None
    from server.routers.search import group_rank   # 惰性导入: 避免模块加载期牵连 server

    def key(x):
        pr = group_rank(x.get("name"), cfg)
        return (0 if pr is None else 1,
                -pr if pr is not None else 0,   # reverse 下 -pr 越大越靠前 → 序号小的先
                x.get("seeders") or 0,
                x.get("size") or 0)
    return sorted(items, key=key, reverse=True)[0]


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
    """对已搜到的磁力列表: 筛选(片源/分辨率/发布组/大小)→ 挑最优(前排组优先)→ (可选)推 CD2。

    返回 (pushed: bool, note: str)。pushed=True 表示已推 CD2。
    """
    if not items:
        return False, "无磁力"
    hit = _pick_best([it for it in items if _passes(it.get("size"), it.get("name"), settings)],
                     cfg)
    if not hit:
        return False, f"{len(items)} 条磁力, 无符合筛选的({_rule_summary(settings)})"
    if not push_enabled:
        # 干跑: 只记录会推什么, 不真推
        gb = (hit.get("size") or 0) / (1024 ** 3)
        return False, (f"干跑: 将推 {hit.get('name', '')[:40]} "
                       f"({_resolution_class(hit.get('name'))}, {gb:.1f}GB)")
    from clients.clouddrive import client as cd2
    from lib import state as state_mod
    try:
        await asyncio.to_thread(cd2.add_offline, cfg, hit.get("magnet"), to_folder, base_dir)
    except Exception as e:  # noqa: BLE001
        return False, f"推 CD2 失败: {str(e)[:120]}"
    # 记入下载队列(与手动推送同一去重)
    try:
        from scripts.push import build_task
        task = build_task(hit.get("magnet"), {"title": title, "content_type": kind},
                          to_folder)
        state_mod.add_task(task)
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
            log(f"追踪检查: 共 {len(tracks)} 条 (全局自动推送={'开' if settings['auto_push'] else '关'}"
                f", 筛选: {_rule_summary(settings)})")
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
    """同步入口(供调度器线程调用)。cfg 缺省则现读数据库配置(get_config)。"""
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
