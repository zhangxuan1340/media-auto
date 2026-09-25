#!/usr/bin/env python3
"""
media-auto / organize —— 离线目录整理: 清广告 → 改名 → 归位媒体库
================================================================
对离线根(默认 /Offline)下的每个一级条目:

  1. 扫描条目内全部文件
  2. 分类: 广告 | 杂项 | 字幕 | 视频
       - 广告 = 纯推广名(去掉【…www.x.com…】推广块后没有实际片名的),如
         【更多无水印蓝光原盘请访问 www.HDBTHD.com】…】.MP4 / .DOC / .MKV
         这类文件多为 289KB 左右,扩展名五花八门,同一出处的域名有很多变体
       - 杂项 = 非视频非字幕(.txt/.url/.nfo/.jpg/.png/.exe/.doc…)
  3. 用「目录名 + 主媒体文件名」反查元数据(中文名/年份/IMDB/TMDB)
     —— 本地 TMDB 缓存优先(零网络), 未命中走 TMDB 直连(多语言搜索)
  4. 生成规范目录名:  <标题>.<年份>.ttXXXXXXX
                     无 IMDB 时 → <标题>.<年份>.tmdbXXXXXXX
  5. [--apply] 执行: 删广告 → RenameFile 改名 → MoveFile 归位到 /Cloud/<分类>

关联说明: 磁力链本身不带 TMDB/IMDB,所以关联靠「下载后的目录名/文件名」反查。
英文片名(如 Fireflies.in.the.Sun)也能反查到中文条目 —— TMDB 多语言搜索对两种都准。

默认 dry-run(只打印计划,不动任何数据);加 --apply 才真正执行。

用法:
  python3 scripts/organize.py                        # 预览整理计划
  python3 scripts/organize.py --only 误杀             # 只看某个条目
  python3 scripts/organize.py --apply --limit 3       # 真正执行(最多 3 个)
  python3 scripts/organize.py --json > plan.json      # 机器可读的计划
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clients.clouddrive import client as cd2  # noqa: E402
from clients.tmdb import client as tmdb  # noqa: E402  (元数据主源: 反查/详情/英文名)
from lib import naming  # noqa: E402
# 元数据只走 本地 TMDB 缓存 → TMDB 直连两级。

# ---- 阈值默认值(可在 config.organize 里覆盖; 读取一律走下方统一入口, 非法值回退默认) ----
DEFAULT_MIN_MATCH_SCORE = 0.9
DEFAULT_SCAN_DEPTH = 3
DEFAULT_IGNORE_PREFIX = ("_", ".", "[Search]")

# 告警去重: 同一个非法配置值只在进程内告警一次(避免每条整理都刷一遍 stderr)
_CONFIG_WARNED = set()


def _warn_config_once(config, key, raw, expected, default):
    """统一入口的告警: 非法/留空值回退默认时, 把原因打到 stderr(服务日志可见)。"""
    tag = f"organize.{key}"
    if tag in _CONFIG_WARNED:
        return
    _CONFIG_WARNED.add(tag)
    print(f"[config] ⚠ {tag}={raw!r} 非法(要求 {expected}), 回退默认 {default!r} —— "
          f"请修正 config.json 的 organize.{key}", file=sys.stderr)


def min_match_score(config):
    """反查结果与文件名的最低匹配度(统一读取入口)。

    低于它 → 判错配拒绝(实例: 目录名「国安[全12集]…」被搜成《国土安全》)。
    配置: organize.min_match_score, 要求 0 < x <= 1; 留空/非数字/越界 → 回退 0.9 并告警。
    每次调用读 config → **热加载**(get_config 按 mtime 自动重载, 改完即生效)。
    """
    raw = org_cfg(config).get("min_match_score")
    if raw in (None, ""):
        return DEFAULT_MIN_MATCH_SCORE
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _warn_config_once(config, "min_match_score", raw, "0 < x <= 1 的数字",
                          DEFAULT_MIN_MATCH_SCORE)
        return DEFAULT_MIN_MATCH_SCORE
    v = float(raw)
    if not (0 < v <= 1):
        _warn_config_once(config, "min_match_score", raw, "0 < x <= 1",
                          DEFAULT_MIN_MATCH_SCORE)
        return DEFAULT_MIN_MATCH_SCORE
    return v


def scan_depth(config):
    """扫描条目内文件的递归深度(统一读取入口)。

    剧集常见 Show/Season 01/xxx.mkv,3 层足够。
    配置: organize.scan_depth, 要求 1 <= x <= 8 的整数; 非法/留空 → 回退 3 并告警。
    """
    raw = org_cfg(config).get("scan_depth")
    if raw in (None, ""):
        return DEFAULT_SCAN_DEPTH
    if isinstance(raw, bool) or not isinstance(raw, int):
        _warn_config_once(config, "scan_depth", raw, "1..8 的整数", DEFAULT_SCAN_DEPTH)
        return DEFAULT_SCAN_DEPTH
    if not (1 <= raw <= 8):
        _warn_config_once(config, "scan_depth", raw, "1..8", DEFAULT_SCAN_DEPTH)
        return DEFAULT_SCAN_DEPTH
    return raw


def ignore_prefixes(config):
    """忽略的一级条目前缀(统一读取入口)。

    默认 ("_", ".", "[Search]") —— 临时目录/隐藏文件/探测产物。
    配置: organize.ignore_prefix(字符串数组); 留空/非数组/空数组 → 回退默认并告警。
    """
    raw = org_cfg(config).get("ignore_prefix")
    if raw in (None, ""):
        return DEFAULT_IGNORE_PREFIX
    if (not isinstance(raw, (list, tuple))
            or not all(isinstance(p, str) and p for p in raw) or not raw):
        _warn_config_once(config, "ignore_prefix", raw, "非空字符串数组",
                          list(DEFAULT_IGNORE_PREFIX))
        return DEFAULT_IGNORE_PREFIX
    return tuple(raw)


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
def org_cfg(config):
    return config.get("organize") or {}


def offline_root(config):
    return (org_cfg(config).get("offline_root")
            or config.get("clouddrive2", {}).get("offline_root")
            or "/Offline")


def cloud_root(config):
    """媒体最终存放位置(用户约定: /Cloud)。

    /Cloud 下【只允许往里移动,禁止任何删除】—— 该保护由 clients/clouddrive 的
    no_delete_roots() 强制执行(默认取本函数返回的路径)。
    """
    return (org_cfg(config).get("cloud_root")
            or (config.get("clouddrive2", {}) or {}).get("cloud_root")
            or "/Cloud")


def root_for_kind(config, kind):
    """[兼容保留] 电影/剧集各自的媒体库根目录。

    新架构已改为「按分类目录归位」: 目标 = cloud_root/<分类目录>,见 category_of()。
    这个函数只在分类完全不可用时作为兜底(且只用于展示,不再用于移动)。
    """
    roots = org_cfg(config).get("roots") or {}
    if kind == "movie":
        return roots.get("movie") or org_cfg(config).get("movie_root") or "/Cloud/CnMovie"
    if kind == "tv":
        return roots.get("tv") or org_cfg(config).get("tv_root") or "/Cloud/CnShow"
    return None


def build_classify_media(meta, name, media_files):
    """把元数据 + 原始目录/文件名拼成 lib/classify.py 需要的 media dict。

    classify 的级联是: 动画(Dm) > 纪录片(Jl) > 综艺(Xr) > 体育(Sp) >
    音乐(Mu) > 地区(Cn/En/JpKr/Hk/Sea/Ot),而地区判定依赖 original_language +
    制片国家,所以这两个字段必须传全,否则会一律落 Ot(其他)。
    注: 2026-09-22 起不再按 18+ 分级分类(成人内容改由 Jellyfin 按分级控制),
    meta 里的 certification/adult 仍照传,但已不参与归类。
    """
    files = media_files or []
    filename = " ".join([name or ""] + [(f.get("name") or "") for f in files[:3]])
    # ⚠️ 类型判定: meta 优先; **meta 缺失(反查失败)时退回文件名季集标记** ——
    # 否则反查失败的剧集(目录名带 S01E01)会被当 movie 归位进 Movie 目录(剧集专属 bug)。
    if (meta or {}).get("kind"):
        ct = "tv" if meta.get("kind") == "tv" else "movie"
    else:
        ct = "tv" if kind_hint_of(name, *[(f.get("name") or "") for f in files[:3]]) else "movie"
    return {
        "title": (meta or {}).get("title") or name or "",
        "content_type": ct,
        "genres": list((meta or {}).get("genres") or []),
        "language": (meta or {}).get("original_language") or "",
        "countries": list((meta or {}).get("countries") or []),
        "certification": (meta or {}).get("certification") or "",
        "adult": bool((meta or {}).get("adult")),
        "filename": filename,
    }


def category_of(config, meta, name, media_files):
    """用分类引擎决定目标分类目录。

    返回 (folder, category_key, reasons):
      folder        —— 实际目录名(取 config.categories 的映射值,如 JpKrShow -> 'Jp&KrShow')
      category_key  —— 分类键(如 JpKrShow)
      reasons       —— 命中原因,用于在界面上解释「为什么放这里」
    """
    try:
        from lib import classify as _classify
    except Exception:  # noqa: BLE001
        return None, None, []
    media = build_classify_media(meta, name, media_files)
    try:
        res = _classify.classify(media, config)
    except Exception:  # noqa: BLE001
        return None, None, []
    return res.get("folder"), res.get("category_key"), (res.get("reasons") or [])


def _junk_ext(config):
    extra = org_cfg(config).get("junk_ext")
    if not extra:
        return naming.DEFAULT_JUNK_EXT
    return naming.DEFAULT_JUNK_EXT | {e.lower() for e in extra}


# ---- 命名/刮削相关配置 ----
def folder_template(config):
    """目录名模板。默认 TMM 风格 '标题 (年份)'。"""
    return org_cfg(config).get("folder_template") or "{title} ({year})"


def movie_file_template(config):
    """电影文件名模板(不含扩展名)。默认 TMM 风格 '标题 (年份) 1080p h265 AC3'。"""
    return org_cfg(config).get("movie_file_template") or "{title} ({year}) {quality}"


def write_nfo_enabled(config):
    return bool(org_cfg(config).get("write_nfo", True))


def rename_media_enabled(config):
    return bool(org_cfg(config).get("rename_media_file", True))


def tv_file_template(config):
    """集文件名模板(不含扩展名)。默认对齐库内风格 '标题 - S01E01 - 1080p h265 AC3'。"""
    return org_cfg(config).get("tv_file_template") or naming.TV_FILE_TEMPLATE


def local_root(config):
    """CD2 根 "/" 对应的本地挂载点(探测媒体信息用)。"""
    return (org_cfg(config).get("local_root")
            or config.get("clouddrive2", {}).get("local_root"))


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def scan_tree(config, path, base_dir=None, depth=None, _root=None):
    if depth is None:
        depth = scan_depth(config)  # 统一入口: organize.scan_depth(热加载)
    """递归列出 path 下所有文件,返回 [{name, path, size, rel_dir}]。

    rel_dir 是相对条目根目录的所在子目录(剧集常见 'Season 01'),用于归位后定位文件。
    """
    _root = (_root if _root is not None else path.rstrip("/"))
    out = []
    try:
        items = cd2.get_subfiles(config, path, base_dir=base_dir)
    except Exception:  # noqa: BLE001
        return out
    for it in items:
        name = it.get("name") or ""
        fpath = it.get("fullPathName") or f"{path.rstrip('/')}/{name}"
        if cd2.is_dir(it):
            if depth > 0:
                out += scan_tree(config, fpath, base_dir=base_dir, depth=depth - 1, _root=_root)
        else:
            rel = fpath[len(_root):].lstrip("/") if fpath.startswith(_root) else name
            rel_dir = os.path.dirname(rel)
            out.append({"name": name, "path": fpath, "size": int(it.get("size") or 0),
                        "rel_dir": rel_dir})
    return out


def classify_entry_files(config, files):
    """把文件列表分成 ads / junk / nfos / assets / subtitles / media 六桶。

    - ads / junk  → 默认删除(广告、文档、脚本、截图、压缩包等)
    - nfos        → 只在「即将写入我们自己的 NFO」时才替换,否则原样保留
    - assets      → 库内成品资产(poster.jpg / fanart.jpg …),永不删
    - subtitles   → keep_subtitles=false 时才并入待删
    """
    junk_ext = _junk_ext(config)
    ad_disable = set(org_cfg(config).get("ad_disable") or [])
    min_video = int(org_cfg(config).get("min_video_bytes") or 0)
    keep_subtitles = org_cfg(config).get("keep_subtitles", True)

    ads, junk, nfos, assets, subtitles, media = [], [], [], [], [], []
    for f in files:
        cat = naming.classify_file(f["name"], f.get("size") or 0, junk_ext=junk_ext, ad_disable=ad_disable)
        # 小体积"视频"也当广告(可选,默认 0 = 关闭,避免误伤预告/样片)
        if cat == "media" and min_video and (f.get("size") or 0) < min_video:
            cat = "ad"
        if cat == "ad":
            ads.append(f)
        elif cat == "junk":
            junk.append(f)
        elif cat == "nfo":
            nfos.append(f)
        elif cat == "asset":
            assets.append(f)
        elif cat == "subtitle":
            subtitles.append(f)
        elif cat == "media":
            media.append(f)
    # 字幕若配置为不保留,则并入待删
    if not keep_subtitles:
        junk += subtitles
        subtitles = []
    return ads, junk, nfos, assets, subtitles, media


# ---------------------------------------------------------------------------
# 元数据解析
# ---------------------------------------------------------------------------
def kind_hint_of(*names):
    """从目录名/文件名里嗅出 movie / tv。有季集标记(如 S01E01、第07集、全07集)就是剧。"""
    for n in names:
        if n and naming.EP_MARKER.search(n):
            return "tv"
    return None


# 续集序号守卫: 目录标了明确的续集/第N部, 候选标题却没有该序号 → 不是同一部。
_SEQ_CJK = re.compile(r"[\u4e00-\u9fff]+?(\d{1,3})")          # 非诚勿扰3 / 终结者2 / X战警3
_SEQ_WORD = re.compile(r"(?:^|\s)(\d{1,2})(?:\s|$)")           # 独立小数字 "If You Are the One 3"
_TECH_NUM = {"1080", "2160", "720", "480", "360", "4k"}        # 分辨率, 不算序号


def _dir_sequence(title):
    """从(已剥离年份与广告的)标题里抽"续集序号",没有返回 ''。

    只认两种: 中文片名直接后缀数字(非诚勿扰3), 或标题里独立的 1~2 位小数字
    (If You Are the One 3)。分辨率(2160/1080)、年份(已剥离)、带单位的数字(HDR/DDP5.1)都不算。
    """
    if not title:
        return ""
    m = _SEQ_CJK.search(title)
    if m:
        return m.group(1)
    for m in _SEQ_WORD.finditer(title):
        n = m.group(1)
        if n.lower() not in _TECH_NUM:
            return n
    return ""


def resolve_from_local_cache(config, dir_name, cands, year, hint):
    """优先查本地 TMDB 缓存(零网络, 快且分类字段齐)。

    本地缓存以 Jellyfin 库为种子 + 用户浏览时现拉的作品。命中即返回与 resolve_meta_sync
    同形状的 meta —— 含 original_language + countries, 供分类判地区。未命中返回 None,
    由调用方回退 TMDB 直连(无后续兜底)。

    评分口径: 用【目录名】(反查最可靠的信号) 对本地 title/original_title 做 title_match,
    与下游 best_title_match(meta, names) 同口径(目录名也在 names 里), 保证本地命中的条目
    必然也过下游的 min_match_score 校验, 不会出现"本地采纳了但下游判错配"的割裂。
    """
    if not dir_name:
        return None
    min_score = min_match_score(config)
    try:
        from db.database import SessionLocal
        from db.models import TmdbMedia
        from sqlalchemy import or_
    except Exception:  # noqa: BLE001
        return None
    s = SessionLocal()
    try:
        best = None  # (score, meta)
        seen_ids = set()
        # 目录标题里的续集序号(非诚勿扰3 / If You Are the One 3) —— 没有则为 ''
        # 仅电影路径用(剧集的季号是另一回事, 由 kind_hint 处理)。
        _dir_title, _ = naming.extract_title_year(dir_name or "")
        _dir_seq = _dir_sequence(_dir_title)
        # 两梯队候选: 年份吻合的优先; 仅在没有任何年份吻合候选时, 才回退到年份不符
        # (兼容字幕组乱标年份)。避免《基督山伯爵.2024》被同名 1961 版抢走(2026-09-22 实测过的偶发错配)。
        year_matched, year_off = [], []
        for q in cands:
            if not q:
                continue
            like = f"%{q}%"
            rows = s.query(TmdbMedia).filter(
                or_(TmdbMedia.title.like(like), TmdbMedia.original_title.like(like))
            ).limit(50).all()
            for r in rows:
                if r.tmdb_id in seen_ids:
                    continue
                seen_ids.add(r.tmdb_id)
                # kind 冲突(文件名判是剧, 本地是电影) → 否
                if hint and r.kind and r.kind != hint:
                    continue
                # 续集序号守卫(仅电影): 目录明确标了序号(非诚勿扰3 / If You Are the One 3),
                # 候选标题却没有该序号 → 不是同一部(是 2008 第一部), 跳过,
                # 逼出 TMDB 直连去找对的那一部(2026-09-22 《非诚勿扰3》误配 2008 第一部)。
                # 剧集(Sxx 季号)不触发——季号是另一回事。
                if hint != "tv" and _dir_seq:
                    r_seq = _dir_sequence(f"{r.title or ''} {r.original_title or ''}")
                    if r_seq != _dir_seq:
                        continue
                # 用目录名评分(与下游同口径)
                score = max(
                    naming.title_match(r.title or "", dir_name),
                    naming.title_match(r.original_title or "", dir_name),
                )
                if score < min_score:
                    continue
                meta = {
                    "tmdb_id": r.tmdb_id,
                    "imdb_id": r.imdb_id or "",
                    "tvdb_id": r.tvdb_id or None,
                    "title": r.title,
                    "originalTitle": r.original_title or "",
                    "year": r.year or "",
                    "kind": r.kind,
                    "matched_query": q,
                    "original_language": r.original_language or "",
                    # ⚠️ countries 必须带上(tmdb_media.countries, 逗号分隔 ISO 码) ——
                    # 分类判港台(Hk)靠它。港片 original_language 多是 "cn", 若这里只给语言,
                    # 本地缓存路径会把港片判成 CnMovie, 而 TMDB 直连路径(带 countries)判 HkMovie
                    # → 同一部片两次整理进不同目录(2026-09-22 《鼠胆龙威》实测, 已修)。
                    "countries": [c for c in (r.countries or "").split(",") if c],
                    # genres/certification 与 TMDB 直连路径同口径(2026-09-22 补):
                    # genres 供动画/纪录片判定(certification 曾供 18+ 判定, 2026-09-22
                    # 起 18+ 不再分目录, 该字段仅留作展示/兼容, 不再影响归类)。
                    # 缺 genres 会让本地缓存路径的动画退化成纯关键词猜
                    # (2026-09-22 《竹夫人》实测, 已修)。
                    "genres": [int(x) for x in (r.genres or "").split(",") if x.strip().isdigit()],
                    "certification": (r.certification or "").strip(),
                    "_local": True,
                }
                year_ok = not (year and r.year and str(r.year) != str(year))
                (year_matched if year_ok else year_off).append((score, meta))
        # 优先年份吻合的; 都没有才回退年份不符(乱标年份场景)
        pool = year_matched if year_matched else year_off
        if pool:
            pool.sort(key=lambda c: c[0], reverse=True)
            best = pool[0]
        if best:
            best[1]["match_score"] = round(best[0], 2)
            return best[1]
        return None
    except Exception:  # noqa: BLE001
        return None
    finally:
        s.close()


# 手动指定匹配的标记值(matched_query): 前端"重新匹配"选定的条目, 与自动反查区分
MANUAL_MATCH_QUERY = "manual"


def get_manual_match(name):
    """取某离线条目的手动指定匹配(DB, 重启不丢)。返回 meta 摘要或 None。

    存储: TmdbSetting 表 key='org_manual_match:<离线条目名>', value=JSON。
    自动反查会错配(如 基督山伯爵.2024 被反查到 1961 版同名条目), 用户在整理页
    手动选对后, 每次扫描/执行都以这个为准, 直到取消。
    """
    if not name:
        return None
    try:
        from db.database import SessionLocal
        from db import repositories as repo
        import json as _json
        s = SessionLocal()
        try:
            v = repo.get_setting(s, f"org_manual_match:{name}", "")
        finally:
            s.close()
        if not v:
            return None
        d = _json.loads(v)
        if not d.get("tmdb_id"):
            return None
        return {
            "tmdb_id": d.get("tmdb_id"),
            "imdb_id": d.get("imdb_id") or "",
            "tvdb_id": d.get("tvdb_id"),
            "title": d.get("title") or "",
            "originalTitle": d.get("originalTitle") or "",
            "year": d.get("year") or "",
            "kind": d.get("kind") or "movie",
            "matched_query": MANUAL_MATCH_QUERY,
            "original_language": d.get("original_language") or "",
            "countries": d.get("countries") or [],
            # 分类必需字段(与自动反查同口径): 缺了 → 手动指定的动漫/纪录判错类。
            # 老格式(未存这些字段)手动匹配取空, 需在整理页重新指定一次即补全。
            "genres": d.get("genres") or [],
            "certification": d.get("certification") or "",
            "adult": bool(d.get("adult")),
            "match_score": 1.0,
            "_manual": True,
        }
    except Exception:  # noqa: BLE001
        return None


def set_manual_match(name, meta):
    """保存/更新某离线条目的手动指定匹配。meta 为 None/空 = 删除覆盖。"""
    from db.database import SessionLocal
    from db import repositories as repo
    import json as _json
    s = SessionLocal()
    try:
        key = f"org_manual_match:{name}"
        if meta:
            row = repo.set_setting(s, key, _json.dumps({
                "tmdb_id": meta.get("tmdb_id"),
                "imdb_id": meta.get("imdb_id") or "",
                "tvdb_id": meta.get("tvdb_id"),
                "title": meta.get("title") or "",
                "originalTitle": meta.get("originalTitle") or "",
                "year": meta.get("year") or "",
                "kind": meta.get("kind") or "movie",
                "countries": meta.get("countries") or [],
                # 分类必需字段(与自动反查同口径): 落了这些, 手动指定的动漫/纪录才判得对
                "genres": meta.get("genres") or [],
                "certification": meta.get("certification") or "",
                "original_language": meta.get("original_language") or "",
                "adult": bool(meta.get("adult")),
            }, ensure_ascii=False))
            s.commit()
            return row
        s.query(repo.TmdbSetting).filter_by(key=key).delete()
        s.commit()
        return None
    finally:
        s.close()


def resolve_metadata(config, dir_name, media_files, base_dir=None):
    """用目录名 + 最大的几个媒体文件名反查元数据,返回 meta 或 None。

    元数据源优先级(命中即停):
      1. 本地 TMDB 缓存(零网络, 已同步的作品) —— resolve_from_local_cache
      2. TMDB 直连搜索(多语言, 覆盖库里没有的) —— tmdb.resolve_meta_sync
    (只有这两级; 都未命中返回 None=未匹配)

    会做两道校验(见 naming.title_match):
      - 匹配度 < organize.min_match_score → 视为错配,返回 {"rejected": ...}
      - 季集标记显示是剧、反查结果却是电影(或反之) → 也拒绝
    """
    ordered = sorted(media_files, key=lambda f: -(f.get("size") or 0))[:3]
    names = [dir_name] + [os.path.splitext(f["name"])[0] for f in ordered]

    hint = kind_hint_of(*names)
    # 无季集标记 + 单媒体文件 → 按【电影】优先解析: 电影几乎都是单文件, 剧集几乎都有 SxxE 多集。
    # 否则 hint=None 时下游 _pick_card / 本地缓存对 movie/tv 无偏好, 同名同年的剧会抢在电影前面
    # (2026-09-22 《微微一笑很倾城》: 电影文件 Love.O2O.2016 被匹到同名电视剧 66776 而非电影 412190)。
    # ⚠️ 只作为"偏好"(soft): 传电影时若真没有电影候选, _pick_card 仍会回退剧集, 不会漏配。
    pref_kind = hint
    if not hint and len([f for f in (media_files or []) if f]) <= 1:
        pref_kind = "movie"
    year = ""
    for n in names:
        _, y = naming.extract_title_year(n)
        if y:
            year = y
            break

    cands = []
    for n in names:
        t, y = naming.extract_title_year(n)
        for c in naming.title_query_candidates(n, t, y):
            if c and c not in cands:
                cands.append(c)

    # 剧集的"年份"经常是季份(如 S03 标 2014,而剧本身是 2012),用年份硬匹配会挑错,
    # 所以 TV 只在候选查询里带年份、不把它当筛选条件。
    match_year = None if hint == "tv" else (year or None)
    min_score = min_match_score(config)
    # 1) 本地 TMDB 缓存优先(零网络, 已同步的作品秒回, 且带 original_language 供分类)
    meta = resolve_from_local_cache(config, dir_name, cands, year, pref_kind)
    # 2) TMDB 直连搜索(本地没有时); 都未命中 → 返回 None(未匹配)
    if not meta and (config.get("tmdb", {}) or {}).get("api_key"):
        try:
            meta = tmdb.resolve_meta_sync(config, cands, year=match_year, kind=pref_kind)
        except Exception:  # noqa: BLE001
            meta = None
    if not meta:
        return None

    score = naming.best_title_match(meta, names)
    meta["detected_year"] = year
    meta["match_score"] = round(score, 2)

    if score < min_score:
        meta["rejected"] = (f"反查结果与文件名不匹配(命中《{meta.get('title')}》"
                            f"[{meta.get('matched_query')}],匹配度 {score:.2f} < {min_score}) —— 疑似错配,已跳过")
        return meta
    if hint and meta.get("kind") and meta["kind"] != hint:
        meta["rejected"] = (f"文件名含季集标记(应为剧集),反查却是{meta['kind']}《{meta.get('title')}》"
                            f" —— 疑似错配,已跳过")
        return meta
    return meta


# ---------------------------------------------------------------------------
# 生成计划
# ---------------------------------------------------------------------------
def companion_subtitles(config, media_name, siblings):
    """散落视频文件: 找出同目录下同名的字幕文件(便于一起搬走)。"""
    stem = os.path.splitext(media_name)[0].lower()
    out = []
    for s in siblings:
        if cd2.is_dir(s):
            continue
        sname = s.get("name") or ""
        if naming.ext_of(sname) not in naming.SUBTITLE_EXT:
            continue
        sstem = os.path.splitext(sname)[0].lower()
        if sstem == stem or sstem.startswith(stem + ".") or stem.startswith(sstem + "."):
            out.append(s.get("fullPathName") or sname)
    return out


def analyse_entry(config, entry, base_dir=None, siblings=None):
    """分析一个离线条目,返回 plan 字典(status: ok / no_media / unresolved)。"""
    name = entry.get("name") or ""
    path = entry.get("fullPathName") or f"{offline_root(config).rstrip('/')}/{name}"
    is_directory = cd2.is_dir(entry)

    if is_directory:
        files = scan_tree(config, path, base_dir=base_dir)
    else:
        files = [{"name": name, "path": path, "size": int(entry.get("size") or 0), "rel_dir": ""}]
        # 散落文件没有自己的目录,把同目录同名的字幕一并算进来
        for sp in companion_subtitles(config, name, siblings or []):
            files.append({"name": os.path.basename(sp), "path": sp, "size": 0, "rel_dir": ""})

    # 目录里已有 .nfo → 说明已经刮削过(我们自己写的或 TMM 写的)
    has_nfo = any((f.get("name") or "").lower().endswith(".nfo") for f in files)

    ads, junk, nfos, assets, subtitles, media = classify_entry_files(config, files)
    media_bytes = sum(f.get("size") or 0 for f in media)

    plan = {
        "source": path,
        "name": name,
        "is_dir": is_directory,
        "has_nfo": has_nfo,
        "ads": [f["path"] for f in ads],
        "junk": [f["path"] for f in junk],
        "nfos": [f["path"] for f in nfos],
        "assets": [f["path"] for f in assets],
        "subtitles": [f["path"] for f in subtitles],
        "media": [{"name": f["name"], "path": f["path"], "size": f.get("size") or 0,
                   "rel_dir": f.get("rel_dir") or ""} for f in media],
        "ad_files": [{"name": f["name"], "size": f.get("size") or 0} for f in ads + junk],
        "junk_files": [{"name": f["name"], "size": f.get("size") or 0} for f in junk],
        "asset_files": [{"name": f["name"], "size": f.get("size") or 0} for f in assets],
        "media_count": len(media),
        "media_bytes": media_bytes,
        "ad_count": len(ads) + len(junk),
        "meta": None,
        "new_name": None,
        "target_root": None,
        "target": None,
        "status": "ok",
        "reason": "",
    }

    if not media:
        plan["status"] = "no_media"
        plan["reason"] = "没有视频文件(可能还在下载中)"
        return plan

    # 手动指定匹配优先(整理页"重新匹配"选定, 持久化在 DB) —— 自动反查会错配
    # (如 基督山伯爵.2024 被反查到 1961 版同名条目), 手动选定的以它为准。
    meta = get_manual_match(name) or resolve_metadata(config, name, media, base_dir=base_dir)
    resolved = bool(meta) and not meta.get("rejected")

    if resolved:
        plan["meta"] = {
            "title": meta.get("title"),
            "year": meta.get("year"),
            "imdb_id": meta.get("imdb_id") or "",
            "tmdb_id": meta.get("tmdb_id"),
            "kind": meta.get("kind"),
            "matched_query": meta.get("matched_query"),
            "match_score": meta.get("match_score"),
        }
        # 目录名按模板生成(默认 TMM 风格 '标题 (年份)';
        # 想回到 '标题.年份.ttID' 只需把 config.organize.folder_template 改成 '{title}.{year}.{imdb}')
        plan["new_name"] = naming.folder_name_from_meta(meta, folder_template(config))
    else:
        # 反查不到 → 不改名、沿用原名(避免把好名字改坏)
        plan["meta"] = None
        plan["new_name"] = name
        if meta and meta.get("rejected"):
            plan["reason"] = meta["rejected"]
        elif has_nfo or naming.is_normalized(name):
            plan["reason"] = "反查未命中,按原名归位"
        else:
            plan["reason"] = "反查未匹配到条目"

    # ---- 分类 → 目标 = /Cloud/<分类目录>(用户约定: /Cloud 下只允许往里移动) ----
    folder, cat_key, reasons = category_of(config, meta if resolved else None, name, media)
    plan["category"] = cat_key
    plan["category_folder"] = folder
    plan["category_reasons"] = reasons

    if folder:
        plan["target_root"] = cloud_root(config).rstrip("/") + "/" + folder
        plan["target"] = plan["target_root"] + "/" + plan["new_name"]
        plan["status"] = "ok"
        if not plan["reason"]:
            plan["reason"] = "分类: " + "/".join(reasons)
        if not resolved:
            plan["reason"] = "改名未采用(" + plan["reason"] + "),仅分类归位"
        return plan

    # 分类不出来 → 不移动(避免污染错误目录),但仍清广告/写 NFO
    plan["status"] = "unresolved"
    if not plan["reason"]:
        plan["reason"] = "无法确定分类目录(缺少可用的类型/地区线索)"
    plan["reason"] += " —— 只清广告,不移动"
    return plan


def build_plans(config, base_dir=None, only=None, limit=None):
    root = offline_root(config)
    ignore_prefix_list = ignore_prefixes(config)
    entries = cd2.get_subfiles(config, root, base_dir=base_dir)

    # 散落视频的文件名主干 —— 同目录下同名的字幕条目不单独出计划(会被视频一起搬走)
    loose_media_stems = set()
    for e in entries:
        if not cd2.is_dir(e) and naming.ext_of(e.get("name")) in naming.VIDEO_EXT:
            loose_media_stems.add(os.path.splitext(e.get("name") or "")[0].lower())

    plans = []
    for e in entries:
        name = e.get("name") or ""
        if not name or name.startswith(ignore_prefix_list):
            continue
        if only and only.lower() not in name.lower():
            continue
        # 纯字幕散落条目: 若与某个散落视频同名,则跳过(交给视频条目处理)
        if not cd2.is_dir(e) and naming.ext_of(name) in naming.SUBTITLE_EXT:
            stem = os.path.splitext(name)[0].lower()
            if any(stem == m or stem.startswith(m + ".") or m.startswith(stem + ".") for m in loose_media_stems):
                continue
        plans.append(analyse_entry(config, e, base_dir=base_dir, siblings=entries))
        if limit and len(plans) >= limit:
            break

    # 目标已在媒体库里 → 先比季(剧集)、再比版本(电影/同季),避免"永远扫到、永远跳过"。
    # ⚠️ 剧集必须先看【季】: 库里有同剧目录(哪怕只有 S02)不代表 S01 重复 ——
    # 离线含库内没有的季要补季合并(merge),不能拿"离线 S01 单集 2.8G vs 库内 S02 单集 1.9G"
    # 误判成"升级版"而静默丢弃,更不能因 on_conflict=skip 让整季进不了库。
    for p in plans:
        _mark_dedup(config, p, base_dir=base_dir)
    return plans


def _season_hint_from_dir(rel_dir):
    """从所在【子目录名】推断季号: 先认 'Season N'/'第N季'/'Specials',
    再退而认目录名里的裸 Sxx(如 '剧名.S01.1080p.../' 里的 S01 → 1)。

    ⚠️ 多季合集里集文件名常只写 'E01.mkv'(不带 Sxx), 季号只体现在子目录名;
    旧逻辑只认 'Season N' 形式 → 把 '剧名.S01.1080p.../E01.mkv' 当成"无季"留在原地,
    于是旧季包目录搬空/搬一半后仍残留(用户反馈"整理后还留着旧文件夹")。
    """
    d = os.path.basename((rel_dir or "").rstrip("/"))
    if not d:
        return None
    s = naming.season_of_dirname(d)
    if s is None:
        s, _e = naming.parse_episode(d)
    return s


# 裸集号(无季号): 'E01' / 'EP05' / 'Show.E07' → 1/5/7 —— 季号由子目录名(见上)提供。
_BARE_EP_RE = re.compile(r"(?:^|[.\s_\-\[\(])E(?:P)?\s?(\d{1,3})(?!\d)", re.IGNORECASE)


def _episode_hint(name):
    """文件主干里的裸集号(不带 Sxx): 多季合集里常见 '剧名.S01.1080p/ E01.mkv' 这种命名,
    原名解析不出集号 → 搬进 Season N 后也无法规范改名。取不到返回 None。"""
    stem = os.path.splitext(name or "")[0]
    m = _BARE_EP_RE.search(stem)
    return int(m.group(1)) if m else None


def _offline_seasons(config, plan):
    """离线条目覆盖的季号集合。目录条目扫全部视频文件;散落文件取自身文件名季号。"""
    srcs = []
    if plan.get("is_dir"):
        try:
            srcs = [f for f in scan_tree(config, plan.get("source"), base_dir=None)
                    if naming.ext_of(f.get("name")) in naming.VIDEO_EXT]
        except Exception:  # noqa: BLE001
            srcs = []
    else:
        for f in plan.get("media") or []:
            if naming.ext_of(f.get("name") or "") in naming.VIDEO_EXT:
                srcs.append(f)
    seasons = set()
    for f in srcs:
        season, _ep = naming.parse_episode(f.get("name") or "")
        if season is None:
            season = _season_hint_from_dir(f.get("rel_dir"))
        if season is not None:
            seasons.add(season)
    return seasons


def _library_seasons(config, dir_path):
    """库内剧目录里已有的季号集合(Season N / 视频文件名 Sxx / Specials)。"""
    seasons = set()
    try:
        for f in scan_tree(config, dir_path):
            d = f.get("rel_dir") or ""
            for part in d.split("/"):
                s = naming.season_of_dirname(part)
                if s is None:
                    s, _e = naming.parse_episode(part)   # 目录名里的裸 Sxx
                if s is not None:
                    seasons.add(s)
            season, _ep = naming.parse_episode(f.get("name") or "")
            if season is not None:
                seasons.add(season)
    except Exception:  # noqa: BLE001
        pass
    return seasons


def _mark_dedup(config, p, base_dir=None):
    """对 status=ok 的计划做"库内已有同名"判定,改 status 为 merge/upgrade/duplicate。"""
    if p["status"] != "ok":
        return
    tgt, root = p.get("target"), p.get("target_root")
    if not tgt or not root:
        return
    try:
        hit = cd2.find_file_by_path(
            config, root, os.path.basename(tgt.rstrip("/")), base_dir=base_dir)
    except Exception:  # noqa: BLE001
        hit = None
    if not hit:
        return
    if not cd2.is_dir(hit):
        return  # 目标是个文件(如电影散落文件)→ 归 apply_plan 的 on_conflict 处理

    p["existing"] = hit.get("fullPathName") or tgt

    # 剧集: 先比季 —— 离线有库内没有的季 → 补季合并(merge)
    if (p.get("meta") or {}).get("kind") == "tv":
        off_seasons = _offline_seasons(config, p)
        if off_seasons:
            missing = off_seasons - _library_seasons(config, p["existing"])
            if missing:
                p["status"] = "merge"
                p["missing_seasons"] = sorted(missing)
                p["reason"] = (f"库内已有该剧({p['existing']}),缺 "
                               + "、".join(naming.season_dir_name(s) for s in sorted(missing))
                               + " → 补季合并(已有内容不动,只补缺的季)")
                return
            # 离线季全是库内已有 → 走版本比较(同季换片)

    # 同季/电影: 版本比较 —— 同名不等于重复,离线那条可能是更高规格的升级版
    # (如库里 5.3GB 2160p WEB-DL、离线 16.6GB UHD BluRay),只按目录名判重会把升级版丢掉。
    cmp_result = _compare_with_library(config, p, hit, base_dir=base_dir)
    if cmp_result and cmp_result.get("verdict") == "upgrade":
        p["status"] = "upgrade"
        p["version_cmp"] = cmp_result
        p["reason"] = (f"库内已有同名条目,但这条是【升级版】: {cmp_result['why']}"
                       f"  (库内: {p['existing']})")
    else:
        p["status"] = "duplicate"
        if cmp_result:
            p["version_cmp"] = cmp_result
            p["reason"] = (f"媒体库已有同名条目且规格不低,不重复归位: {tgt}"
                           f"  [{cmp_result['why']}]")
        else:
            p["reason"] = f"媒体库已有同名条目,不重复归位: {tgt}"


def _library_dir_version(config, dir_path, base_dir=None):
    """读库里已有条目的「主媒体文件」(体积最大的那个视频),返回 (name, size)。

    库内条目可能是目录(常见)或散落文件;读不到返回 None。
    """
    files = scan_tree(config, dir_path, base_dir=base_dir)
    vids = [f for f in files if naming.ext_of(f.get("name")) in naming.VIDEO_EXT]
    if not vids:
        return None
    biggest = max(vids, key=lambda f: f.get("size") or 0)
    return biggest.get("name") or "", int(biggest.get("size") or 0)


def _compare_with_library(config, plan, hit, base_dir=None):
    """把离线条目与库里同名条目做版本比较,返回 compare_versions 的结果(读不到则 None)。"""
    try:
        from lib import mediainfo
    except Exception:  # noqa: BLE001
        return None

    src = max(plan.get("media") or [{}], key=lambda f: f.get("size") or 0)
    new_name = src.get("name") or ""
    new_size = int(src.get("size") or 0)
    if not new_name:
        return None

    lib = _library_dir_version(config, hit.get("fullPathName") or "", base_dir=base_dir)
    if not lib:
        return None
    old_name, old_size = lib
    return mediainfo.compare_versions(new_name, new_size, old_name, old_size)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def clean_media_names(config, plan, base_dir=None, log=print):
    """把媒体/字幕文件名里的推广块剥掉(保留技术信息),返回改了几个。

    例: 【高清影视之家发布 www.HDBTHD.com】误杀2[国语中字].Fireflies...-DreamHD.mkv
     →  误杀2[国语中字].Fireflies...-DreamHD.mkv
    只删「含域名的括号块」与裸域名,不动分辨率/编码/发布组这些技术标记。
    同名冲突时跳过,避免覆盖。**会同步更新 plan 里的 name/path**,后续步骤才拿得到新名字。
    """
    changed = []
    targets = list(plan.get("media") or [])
    for p in (plan.get("subtitles") or []):
        targets.append({"name": os.path.basename(p), "path": p})
    for f in targets:
        old_name = f.get("name") or ""
        path = f.get("path") or ""
        if not old_name or not path or not naming.has_promo(old_name):
            continue
        stem, ext = os.path.splitext(old_name)
        new_name = naming.sanitize(naming.strip_promos(stem) + ext)
        if not new_name or new_name == old_name or naming.has_promo(new_name):
            continue
        parent = os.path.dirname(path.rstrip("/")) or "/"
        if cd2.find_file_by_path(config, parent, new_name, base_dir=base_dir):
            continue  # 目标名已被占用,跳过
        try:
            cd2.rename_file(config, path, new_name, base_dir=base_dir)
        except Exception:  # noqa: BLE001
            continue
        changed.append((old_name, new_name))
        f["name"] = new_name
        f["path"] = parent.rstrip("/") + "/" + new_name
    return changed


# ---------------------------------------------------------------------------
# 探测 + 命名 + NFO
# ---------------------------------------------------------------------------
def relocated(plan, final_dir, old_path):
    """把条目内的旧路径映射成归位后的新路径。

    条目 /Offline/旧名/sub/a.mkv 归位到 /Movie/新名 后 -> /Movie/新名/sub/a.mkv
    (散落文件 /Offline/a.mkv 与其同目录字幕 -> final_dir/a.mkv)
    """
    src = (plan.get("source") or "").rstrip("/")
    old = (old_path or "").rstrip("/")
    if src and old.startswith(src + "/"):
        rel = old[len(src) + 1:]
    else:
        rel = os.path.basename(old)
    return final_dir.rstrip("/") + "/" + rel


def _probe_source_media(config, plan, base_dir=None):
    """探测条目里最大的视频文件 —— **用它在离线目录里的原始路径**。

    为什么必须移动前探测: WebDAV 账号只开到 `/Temp`,文件一旦搬到 `/Cloud` 就够不到了
    (本地挂载不存在时尤其致命),于是 `<fileinfo>` 会整个丢失。
    所以先探、把结果缓存进 plan,归位后直接复用。
    """
    media = plan.get("media") or []
    if not media:
        return None
    biggest = max(media, key=lambda f: f.get("size") or 0)
    path = biggest.get("path")
    if not path:
        return None
    from lib import mediainfo
    try:
        timeout = int((config.get("webdav") or {}).get("timeout") or 180)
    except Exception:  # noqa: BLE001
        timeout = 180
    try:
        return mediainfo.probe(path, config, base_dir=base_dir, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None


def _probe_main_media(config, plan, final_dir, base_dir=None):
    """探测条目里最大的那个视频文件(取媒体技术信息)。失败返回 None。"""
    media = plan.get("media") or []
    if not media:
        return None
    biggest = max(media, key=lambda f: f.get("size") or 0)
    from lib import mediainfo
    try:
        timeout = int((config.get("webdav") or {}).get("timeout") or 180)
    except Exception:  # noqa: BLE001
        timeout = 180
    return mediainfo.probe(relocated(plan, final_dir, biggest.get("path")),
                           config, base_dir=base_dir, timeout=timeout)


def _rename_media_for_movie(config, plan, final_dir, meta, quality, base_dir=None, log=print):
    """电影: 把视频文件(连同同名外挂字幕)改成 TMM 风格 '标题 (年份) 质量.ext'。

    只处理「仅 1 个视频文件」的情况;多文件(剧集/多版本)不动,避免误伤。
    返回 {旧 basename: 新 basename}。
    """
    media = plan.get("media") or []
    if len(media) != 1:
        return {}
    f = media[0]
    old_name = f.get("name") or ""
    stem, ext = os.path.splitext(old_name)
    if ext.lower() not in naming.VIDEO_EXT:
        return {}
    new_base = naming.render_template(
        movie_file_template(config), **naming.name_fields(meta, quality))
    if not new_base:
        return {}
    new_name = new_base + ext
    if new_name == old_name:
        return {}

    cur_path = relocated(plan, final_dir, f["path"])
    parent = os.path.dirname(cur_path) or "/"
    if cd2.find_file_by_path(config, parent, new_name, base_dir=base_dir):
        log(f"    ⚠ 目标文件名已存在,不改名: {new_name[:60]}")
        return {}
    try:
        cd2.rename_file(config, cur_path, new_name, base_dir=base_dir)
    except Exception as e:  # noqa: BLE001
        log(f"    ⚠ 视频改名失败: {str(e)[:120]}")
        return {}

    renamed = {old_name: new_name}
    f["name"] = new_name
    f["path"] = parent.rstrip("/") + "/" + new_name

    # 同名外挂字幕跟着改,保证 Jellyfin 仍能关联
    for p in list(plan.get("subtitles") or []):
        sname = os.path.basename(p)
        scur = relocated(plan, final_dir, p)
        if os.path.dirname(scur) != parent:
            continue
        sstem, sext = os.path.splitext(sname)
        if sstem != stem and not (sstem.startswith(stem + ".") or stem.startswith(sstem + ".")):
            continue
        suffix = sstem[len(stem):] if sstem.startswith(stem) else ""
        try:
            cd2.rename_file(config, scur, new_base + suffix + sext, base_dir=base_dir)
            renamed[sname] = new_base + suffix + sext
        except Exception:  # noqa: BLE001
            pass
    return renamed


def _prune_empty_dirs(config, root, base_dir=None, log=print, max_depth=5):
    """递归删除 root 下的【空目录】—— 只用于离线工作区(离线根 /Temp, 可删)。

    多季合集按季归位后, 旧季包子文件夹会被搬空; 若不清理, 整个条目目录搬到
    /Cloud 后这些空壳会残留进媒体库(而 /Cloud 禁删, 事后清不掉), 用户看到
    "整理后还留着旧文件夹"。自底向上删: 先删最深的空目录, 父目录随之变空也一起删。
    **绝不动 root 本身, 也绝不动非空目录。** 返回被删目录路径列表。
    """
    removed = []

    def walk(path, depth):
        if depth > max_depth:
            return False
        try:
            kids = cd2.get_subfiles(config, path, base_dir=base_dir)
        except Exception:  # noqa: BLE001
            return False
        empty = True
        for k in kids:
            if cd2.is_dir(k):
                child = k.get("fullPathName") or ""
                if child and walk(child, depth + 1):
                    try:
                        cd2.delete_files(config, [child], base_dir=base_dir)
                        removed.append(child)
                    except Exception:  # noqa: BLE001
                        empty = False
                else:
                    empty = False
            else:
                empty = False
        return empty

    walk(root.rstrip("/"), 0)
    if removed:
        log(f"    🧹 清理搬空的旧目录 {len(removed)} 个: "
            + "、".join(os.path.basename(x) for x in removed[:4])
            + ("…" if len(removed) > 4 else ""))
    return removed


def _ensure_path(config, path, base_dir=None):
    """递归确保 CD2 上整条目录链存在(客户端只有单层 ensure_folder), 返回规范路径。"""
    cur = ""
    for p in (path or "").strip("/").split("/"):
        if not p:
            continue
        cd2.ensure_folder(config, cur or "/", p, base_dir=base_dir)
        cur = (cur + "/" + p) if cur else ("/" + p)
    return cur or "/"


def _organize_in_workspace(config, plan, work_dir, base_dir=None, log=print):
    """在【离线工作区】(/Temp 离线下载目录, 可删)内把本条整理到位。

    用户铁律: "程序在 Temp 离线下载目录里面整理好, 再移动到对应的 Cloud 目录中去。"
    原因: `/Cloud` 只准往里移、禁删 —— 任何整理(按季归位、改名、删空壳目录)都必须在**可删**的
    工作区里做完, 否则搬空的旧目录/半成品名一旦进了库就再也清不掉。

    剧集: 按季归位 + 集文件标准改名(字幕跟随) + 删掉搬空的旧季包目录。
    电影: 视频文件(连同同名外挂字幕)改成 '标题 (年份) 质量.ext'。
    最后统一 `_prune_empty_dirs` 清掉空目录(空目录进了 /Cloud 永远删不掉)。

    work_dir 必须是【文件当前真正所在】的目录(改名后的工作目录 / 源目录 / 新建的工作目录),
    否则 relocated() 会定位错。返回 {旧 basename: 整理后路径}; 失败只记日志不阻断归位。
    """
    out = {}
    if rename_media_enabled(config):
        meta = plan.get("meta") or {}
        if meta:
            kind = meta.get("kind") or ""
            q = quality_of_plan(config, plan)
            try:
                if kind == "tv":
                    out = _reorganize_tv(config, plan, work_dir, meta, q,
                                         base_dir=base_dir, log=log) or {}
                else:
                    out = _rename_media_for_movie(config, plan, work_dir, meta, q,
                                                  base_dir=base_dir, log=log) or {}
                    if out:
                        log(f"    ✎  视频改名 → {list(out.values())[0][:70]}")
            except Exception as e:  # noqa: BLE001
                log(f"    ⚠ 工作区整理失败(继续归位): {str(e)[:150]}")
    try:
        _prune_empty_dirs(config, work_dir, base_dir=base_dir, log=log)
    except Exception as e:  # noqa: BLE001
        log(f"    ⚠ 清理空目录失败(继续归位): {str(e)[:120]}")
    return out


def _reorganize_tv(config, plan, final_dir, meta, quality, base_dir=None, log=print):
    """剧集: 按季归位 + 集文件标准改名(字幕跟随)。

    在剧目录已归位到 final_dir 之后调用。安全规则:
      - 季号来源: 文件名 SxxExx > 文件名「第N集」+ Sxx > 所在子目录名(Season N/Specials);
        **提取不到季的文件留在原地**, 不乱动。
      - 集号: SxxExx / 第N集; 整季包(只有 Sxx)改 '标题 - S01 - 质量'。
      - 目标名已存在(库内已有同集) → 跳过改名, 必要时按原名移动, **绝不覆盖**。
      - 字幕: 同名主干(或自带 SxxExx)跟随对应集视频进季目录并跟着改名; 对不上的留原地。

    返回 {旧 basename: 新完整路径}(视频 + 字幕)。
    """
    from lib import mediainfo
    title = meta.get("title") or ""
    media = [f for f in (plan.get("media") or [])
             if naming.ext_of(f.get("name") or "") in naming.VIDEO_EXT]
    if not media:
        return {}
    template = tv_file_template(config)
    q = (quality or "").strip() or mediainfo.quality_from_name(
        max(media, key=lambda f: f.get("size") or 0).get("name") or "")

    season_dirs, season_listed = {}, {}

    def season_dir_for(season):
        if season not in season_dirs:
            season_dirs[season] = cd2.ensure_folder(
                config, final_dir, naming.season_dir_name(season), base_dir=base_dir)
        return season_dirs[season]

    def existing_in(season):
        if season not in season_listed:
            try:
                season_listed[season] = {
                    (f.get("name") or "").lower()
                    for f in cd2.get_subfiles(config, season_dirs[season], base_dir=base_dir)
                    if not cd2.is_dir(f)}
            except Exception:  # noqa: BLE001
                season_listed[season] = set()
        return season_listed[season]

    def parse_with_hint(f):
        name = f.get("name") or ""
        season, episode = naming.parse_episode(name)
        if season is None:
            season = _season_hint_from_dir(f.get("rel_dir"))
        if episode is None:
            episode = _episode_hint(name)
        return season, episode

    moved = {}
    batch_taken = set()          # 本批已占用的季目录内文件名(小写)

    # ---- 视频文件: 归季目录 + 改名 ----
    for f in media:
        name = f.get("name") or ""
        ext = naming.ext_of(name)
        f["_orig_stem"] = os.path.splitext(name)[0].lower()   # 改名前主干, 字幕跟随匹配用
        season, episode = parse_with_hint(f)
        f["_season"], f["_episode"] = season, episode
        if season is None:
            log(f"    ⚠ 无季信息,保留原地: {name[:60]}")
            continue
        sd = season_dir_for(season)
        cur = relocated(plan, final_dir, f.get("path") or name)

        # 目标名: 有集号 → 标准集名; 整季包 → '标题 - Sxx - 质量'; 无集号 → 原名
        new_name = name
        if episode is not None:
            cand = naming.tv_episode_filename(title, season, episode, q, template) + ext
            if cand != name:
                new_name = cand
        elif naming._SEASON_ONLY_RE.search(name) and not naming._SEASON_EP_RE.search(name):
            cand = naming.tv_episode_filename(title, season, None, q, template) + ext
            if cand != name:
                new_name = cand

        # 目标名冲突(库内已有同集 / 本批已占用) → 放弃改名, 按原名移动, 不覆盖
        if new_name != name:
            if new_name.lower() in existing_in(season) or new_name.lower() in batch_taken:
                if os.path.dirname(cur).rstrip("/") == sd.rstrip("/"):
                    continue  # 已在季目录且改名会撞名 → 原样保留
                log(f"    ⚠ 目标名已占用,按原名移动: {new_name[:50]}")
                new_name = name

        if os.path.dirname(cur).rstrip("/") == sd.rstrip("/") and os.path.basename(cur) == new_name:
            continue  # 已就位
        if os.path.dirname(cur).rstrip("/") != sd.rstrip("/"):
            cd2.move_file(config, cur, sd, base_dir=base_dir)
            cur = sd.rstrip("/") + "/" + name
        if os.path.basename(cur) != new_name:
            cd2.rename_file(config, cur, new_name, base_dir=base_dir)
            cur = os.path.dirname(cur) + "/" + new_name
        batch_taken.add(new_name.lower())
        f["name"] = new_name
        f["path"] = cur
        moved[name] = cur

    if moved:
        log(f"    🗂 按季归位 {len(moved)} 个视频文件(Season 目录: "
            f"{', '.join(naming.season_dir_name(s) for s in sorted(season_dirs))})")
        for old, new in list(moved.items())[:3]:
            log(f"       → {os.path.basename(new)[:70]}")

    # ---- 字幕跟随: 同集/同主干的进对应季目录并跟着改名 ----
    for sp in list(plan.get("subtitles") or []):
        sname = os.path.basename(sp)
        sstem, sext = os.path.splitext(sname)
        sseason, _sep = naming.parse_episode(sname)
        vf, v_old_stem = None, ""
        if sseason is None:
            # 字幕名无季集 → 按【改名前】主干找对应视频, 沿用它的季集
            for f in media:
                mstem = f.get("_orig_stem") or ""
                if not mstem:
                    continue
                if sstem.lower() == mstem \
                        or sstem.lower().startswith(mstem + ".") \
                        or mstem.startswith(sstem.lower() + "."):
                    sseason, vf, v_old_stem = f.get("_season"), f, mstem
                    break
        elif vf is None:
            # 字幕自带 SxxExx → 按【改名前】主干找同集视频, 用于跟着改名
            for f in media:
                mstem = f.get("_orig_stem") or ""
                if not mstem:
                    continue
                if sstem.lower() == mstem \
                        or sstem.lower().startswith(mstem + ".") \
                        or mstem.startswith(sstem.lower() + "."):
                    vf, v_old_stem = f, mstem
                    break
        if sseason is None:
            continue
        sd = season_dir_for(sseason)
        cur = relocated(plan, final_dir, sp)
        if os.path.dirname(cur).rstrip("/") == sd.rstrip("/"):
            continue  # 已在季目录
        cd2.move_file(config, cur, sd, base_dir=base_dir)
        cur = sd.rstrip("/") + "/" + sname
        # 跟着视频改名: 新视频主干 + 原字幕尾巴(.chs / .zh 等)
        if vf is not None and v_old_stem:
            new_video_stem = os.path.splitext(vf.get("name") or "")[0]
            tail = ""
            if sstem.lower().startswith(v_old_stem):
                tail = sstem[len(v_old_stem):]
            elif sstem.lower().startswith(new_video_stem.lower()):
                tail = sstem[len(new_video_stem):]
            # 主干完全一致时 tail 为空也照改 —— 否则字幕名与视频名对不上(观感/配对都差)
            if new_video_stem and new_video_stem.lower() != sstem.lower():
                new_sname = new_video_stem + tail + sext
                if new_sname != sname \
                        and new_sname.lower() not in existing_in(sseason) \
                        and new_sname.lower() not in batch_taken:
                    try:
                        cd2.rename_file(config, cur, new_sname, base_dir=base_dir)
                        cur = os.path.dirname(cur) + "/" + new_sname
                        batch_taken.add(new_sname.lower())
                    except Exception:  # noqa: BLE001
                        pass
        moved[sname] = cur
    return moved


def _jellyfin_ids_for(config, name, kind):
    """按条目名查 Jellyfin 本地条目的 ProviderIds(库内权威, 任何路径都可读)。

    返回 {imdb, tmdb, tvdb}(小写)。Jellyfin 里没有该条目 → {}(新入库, 无冲突可言)。
    """
    try:
        jf = config.get("jellyfin") or {}
        url = (jf.get("url") or "").rstrip("/")
        tok = jf.get("token") or ""
        if not url or not tok or not name:
            return {}
        itype = "Series" if kind == "tv" else "Movie"
        import urllib.parse
        path = ("/Items?SearchTerm=" + urllib.parse.quote(name)
                + "&IncludeItemTypes=" + itype + "&Recursive=true"
                + "&Fields=ProviderIds,Path&Limit=20")
        req = urllib.request.Request(url + path,
                                     headers={"Authorization": "MediaBrowser Token=" + tok})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        ids = {}
        for it in (d.get("Items") or []):
            # 名字要能对上(精确或包含), 防止 SearchTerm 模糊命中别的片
            n = it.get("Name") or ""
            if n != name and name not in n and n not in name:
                continue
            for k_src, k_dst in (("Imdb", "imdb"), ("Tmdb", "tmdb"), ("Tvdb", "tvdb")):
                v = ((it.get("ProviderIds") or {}).get(k_src) or "").strip().lower()
                if v:
                    ids.setdefault(k_dst, v)
            if ids:
                break
        return ids
    except Exception:  # noqa: BLE001
        return {}


def _nfo_existing_ids(config, final_dir, nfo_name, base_dir=None,
                      name_hint="", kind="movie"):
    """取库内已有条目的 IDs,用于更新前冲突校验。返回 (ids, title)。

    来源优先级: **Jellyfin 本地条目**(本地权威, 全路径可读) → WebDAV 读回 NFO 解析。
    CD2 无读文件接口, /Cloud 上 WebDAV 账号也够不到 —— 两级都拿不到时返回 ({}, ""),
    调用方按"无有效 ID"处理(仅靠标题匹配度校验)。
    """
    if name_hint:
        jf_ids = _jellyfin_ids_for(config, name_hint, kind)
        if jf_ids:
            return jf_ids, name_hint
    path = (final_dir or "").rstrip("/") + "/" + nfo_name
    try:
        wd = config.get("webdav") or {}
        base = (wd.get("base") or "").rstrip("/")
        root = (wd.get("account_root") or "/Temp").rstrip("/")
        if not base or not path.startswith(root + "/"):
            return {}, ""  # 账号根之外(如 /Cloud 越权)读不回 → 按"无 ID"处理
        url = base + path[len(root):]
        req = urllib.request.Request(url, headers={"User-Agent": "media-auto/1.0"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {}, ""
    try:
        root = ET.fromstring(raw)
    except Exception:  # noqa: BLE001
        return {}, ""

    def txt(tag):
        e = root.find(tag)
        return (e.text or "").strip() if e is not None and e.text else ""

    ids = {}
    for u in root.findall("uniqueid"):
        t = (u.get("type") or "").lower()
        v = (u.text or "").strip().lower()
        if t in ("imdb", "tmdb", "tvdb", "wikidata") and v:
            ids[t] = v
    for tag, key in (("imdbid", "imdb"), ("tmdbid", "tmdb")):
        v = txt(tag).lower()
        if v and key not in ids:
            ids[key] = v
    guide = txt("episodeguide")
    if guide:
        try:
            g = json.loads(guide)
            for k, v in (g or {}).items():
                if k.lower() in ("imdb", "tmdb", "tvdb", "wikidata") and v:
                    ids.setdefault(k.lower(), str(v).strip().lower())
        except Exception:  # noqa: BLE001
            pass
    return ids, txt("title")


def _nfo_update_allowed(config, plan, meta, final_dir, nfo_name, base_dir=None, log=print):
    """写 NFO 前的【统一校验闸门】: 缺失 → 写; 已存在但老化 → 更新; 校验不过 → 拒写。

    为什么必须有闸门(2026-09-18 巴比伦柏林事故): 曾无校验直接覆盖库内 NFO,
    一次反查/手动填错 TMDB ID(55172=希腊剧)就把库的标题/季全部刮坏。
    规则:
      - 新写(库里没有 NFO): 直接写 —— 元数据质量由反查阶段(reject/min score)保证,
        这里不二次设卡(否则 unresolved 但目录名规范的条目会缺 NFO)
      - 更新(库里已有 NFO, **老化就要刷**): 新 ID 必须与已有 ID **无冲突**(同一来源
        不得两个值) + 匹配度 >= organize.min_match_score; 已有 NFO 没有效 ID(读不回/
        第三方无 ID)时仅要求匹配度 —— 校验不过一律保留旧 NFO, 绝不带错覆盖
    """
    min_score = min_match_score(config)
    score = naming.best_title_match(
        meta, [plan.get("name") or "", plan.get("new_name") or "",
               plan.get("origin_filename") or ""])
    try:
        hit = cd2.find_file_by_path(config, final_dir, nfo_name, base_dir=base_dir)
    except Exception:  # noqa: BLE001
        hit = None
    if not hit:
        return True  # 新写

    ex_ids, _ex_title = _nfo_existing_ids(
        config, final_dir, nfo_name, base_dir=base_dir,
        name_hint=(plan.get("meta") or {}).get("title") or plan.get("new_name") or "",
        kind=meta.get("kind") or "")
    if ex_ids:
        # 已有 ID → 只允许"兼容更新": 冲突(同一来源 ID 不同) = 反查错了, 拒写保护库内
        new_imdb = (meta.get("imdb_id") or "").strip().lower()
        new_tmdb = str(meta.get("tmdb_id") or "").lower()
        conflicts = []
        if new_imdb and ex_ids.get("imdb") and new_imdb != ex_ids["imdb"]:
            conflicts.append(f"imdb {ex_ids['imdb']}→{new_imdb}")
        if new_tmdb and ex_ids.get("tmdb") and new_tmdb != ex_ids["tmdb"]:
            conflicts.append(f"tmdb {ex_ids['tmdb']}→{new_tmdb}")
        if conflicts:
            log(f"    ⚠  NFO 已有 ID 与新反查冲突({', '.join(conflicts)}) → 保留旧 NFO 不更新"
                "(反查可能错配, 库内 NFO 是本地权威)")
            return False
        if score < min_score:
            log(f"    ⚠  更新 NFO 校验未过(匹配度 {score:.2f} < {min_score}),保留旧 NFO")
            return False
        log(f"    ♻  更新已有 NFO(老化刷新, ID 兼容: {', '.join(sorted(ex_ids))})")
        return True
    # 已有 NFO 但读不到/无有效 ID → 保守: 只信自身校验
    if score < min_score:
        log(f"    ⚠  已有 NFO 无有效 ID 且校验未过(匹配度 {score:.2f} < {min_score}),保留不更新")
        return False
    log("    ♻  更新已有 NFO(旧 NFO 无有效 ID, 自身校验通过)")
    return True


def _write_entry_nfo(config, plan, final_dir, meta, info, base_dir=None, log=print):
    """写 NFO(含老化更新)。电影写 '<视频名>.nfo',剧集写 'tvshow.nfo'。

    写前必过 _nfo_update_allowed 闸门: 缺失→写 / 老化且校验通过→更新 / 校验不过→保留旧的。
    返回写入的路径;被闸门拒绝时返回 None。
    """
    from lib import nfo as nfo_mod
    kind = (meta or {}).get("kind") or kind_hint_of(plan.get("name") or "")
    nfo_name = "tvshow.nfo" if kind == "tv" else ""
    if not nfo_name:
        media = plan.get("media") or []
        nfo_name = os.path.splitext(
            (media[0]["name"] if media else (plan.get("new_name") or "movie")))[0] + ".nfo"
    if not _nfo_update_allowed(config, plan, meta, final_dir, nfo_name,
                               base_dir=base_dir, log=log):
        return None

    wikidata = ""
    if org_cfg(config).get("wikidata", True):
        cache = os.path.join(base_dir or ".", "state", "wikidata_cache.json")
        wikidata = nfo_mod.wikidata_id(meta.get("imdb_id"), cache_file=cache)
    locked = bool(org_cfg(config).get("tmm_locked", True))
    dateadded = time.time()

    if kind == "tv":
        xml = nfo_mod.build_tvshow_nfo(meta, wikidata=wikidata, dateadded=dateadded,
                                       tmm_locked=locked,
                                       original_filename=(plan.get("origin_filename") or ""))
        path = final_dir.rstrip("/") + "/tvshow.nfo"
    else:
        media = plan.get("media") or []
        source = ""
        if media:
            from lib import mediainfo  # noqa: WPS433
            source = mediainfo.guess_source(media[0]["name"])
        orig = plan.get("origin_filename") or ""
        xml = nfo_mod.build_movie_nfo(meta, info=info, source=source,
                                      original_filename=orig, dateadded=dateadded,
                                      wikidata=wikidata, tmm_locked=locked)
        path = final_dir.rstrip("/") + "/" + nfo_name

    written = cd2.write_file(config, path, xml, base_dir=base_dir)
    nbytes = len(xml.encode("utf-8"))
    log(f"    📝 写入 NFO → {os.path.basename(path)}  ({nbytes} 字节"
        + (f", 服务端确认 {written}" if written and written != nbytes else "") + ")")
    return path




# 执行期缓存: (kind, tmdb_id) -> 完整元数据。同一轮里避免重复请求。
_FULL_META_CACHE = {}


def resolve_full_meta(config, plan):
    """取 NFO 所需的完整元数据(plan 里只存了摘要,避免预览响应臃肿)。

    主源 TMDB 直连(唯一源)。
    已含 NFO / 目录名已规范的条目(plan["meta"] 为空)返回 None —— 此时不重写 NFO。
    """
    summary = plan.get("meta") or {}
    tmdb_id = summary.get("tmdb_id")
    if not tmdb_id:
        return None
    kind = summary.get("kind") or kind_hint_of(plan.get("name") or "") or "movie"
    key = (kind, tmdb_id)
    if key in _FULL_META_CACHE:
        return _FULL_META_CACHE[key]
    meta = None
    # 主源: TMDB 直连(唯一源)
    if (config.get("tmdb", {}) or {}).get("api_key"):
        try:
            meta = tmdb.detail_sync(config, kind, tmdb_id)
            if org_cfg(config).get("english_title", True):
                # TMM 的 <english_title> 取 TMDB 英文标题(?language=en),
                # 如 等一个人咖啡 -> 'Café. Waiting. Love'
                try:
                    meta["english_title"] = tmdb.english_title_sync(config, kind, tmdb_id)
                except Exception:  # noqa: BLE001
                    meta["english_title"] = ""
        except Exception:  # noqa: BLE001
            meta = None
    if not meta:
        return None
    meta["match_score"] = summary.get("match_score")
    meta["matched_query"] = summary.get("matched_query")
    _FULL_META_CACHE[key] = meta
    return meta


def finalize_entry(config, plan, final_dir, meta, base_dir=None, log=print,
                   skip_media_rename=False):
    """归位之后: 探测 → 媒体改名 → 写 NFO。返回结果字典。

    meta 为 None 时(已规范的条目)跳过 NFO 与改名,只做搬运。
    skip_media_rename=True: 媒体文件已在【离线工作区 /Temp】内整理好(剧集按季归位 / 电影改名,
        见 apply_plan)→ 这里不再重复改名, 只补写 NFO。用户铁律: "先在 Temp 离线目录里整理好,
        再移到对应的 Cloud 目录" —— /Cloud 禁删, 整理必须留在可删的工作区完成。
    """
    out = {"renamed_media": {}, "nfo": None, "probed": False}
    if not meta:
        return out

    quality, info = "", None
    from lib import mediainfo
    if org_cfg(config).get("probe_media", True):
        # 优先用搬运前探测好的结果(那时路径还在 /Temp 下,WebDAV 够得到)
        info = plan.get("_probe_info")
        if not info:
            info = _probe_main_media(config, plan, final_dir, base_dir=base_dir)
        if info:
            out["probed"] = True
            quality = mediainfo.quality_tag(info)
            v = info.get("video") or {}
            ch = {"local": "本地挂载", "webdav": "WebDAV"}.get(info.get("source") or "", info.get("source") or "")
            log(f"    🔎 探测[{ch}]: {quality}  ({info.get('resolution_label')} {v.get('codec_label')}"
                f" {int(info.get('duration') or 0) // 60} 分钟)")
        else:
            # 本地挂载与 WebDAV 都不可用 → 退回从文件名推质量标记(结果通常一致),
            # 只是 NFO 里没有 <fileinfo> 段
            quality = mediainfo.quality_from_name(
                plan.get("origin_filename")
                or (plan.get("media") or [{}])[0].get("name") or "")
            log(f"    ⚠ 未探测到媒体信息(本地挂载与 WebDAV 都不可用) —— 质量标记改用文件名推断: "
                f"{quality or '(无)'}; NFO 不含 <fileinfo>")
    else:
        quality = mediainfo.quality_from_name(plan.get("origin_filename") or "")

    kind = meta.get("kind") or ""
    if rename_media_enabled(config) and not skip_media_rename:
        if kind == "tv":
            # 剧集: 按季建目录(Season N)归位 + 集文件标准改名 '标题 - SxxExx - 质量'
            mv = _reorganize_tv(config, plan, final_dir, meta, quality, base_dir=base_dir, log=log)
            if mv:
                out["renamed_media"] = mv
        else:
            rn = _rename_media_for_movie(config, plan, final_dir, meta, quality, base_dir=base_dir, log=log)
            if rn:
                out["renamed_media"] = rn
                log(f"    ✎  视频改名 → {list(rn.values())[0][:70]}")

    if write_nfo_enabled(config):
        try:
            out["nfo"] = _write_entry_nfo(config, plan, final_dir, meta, info,
                                          base_dir=base_dir, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"    ⚠ NFO 写入失败: {str(e)[:150]}")
    return out


# 补季执行期缓存: ("sd", season) → 库内季目录路径, ("ls", season) → 季目录已有文件名集合。
# 防止同一季反复 get_subfiles; 每个 plan 执行完清空。
_MERGE_CACHE = {}


def _apply_merge_seasons(config, plan, base_dir=None, log=print):
    """剧集补季执行: 清推广名 → 删广告 → 【逐文件】把缺失季的视频/字幕搬入库内对应季目录
    (文件级 MoveFile Skip,绝不覆盖) → 集文件按标准名改名(字幕跟随) → 更新 tvshow.nfo。

    ⚠️ 不用整目录 MoveFile: 实测(巴比伦柏林 2026-09-18)CD2 对「源目录名 == 库内已有目录名」
    的 MoveFile(递归) 是【嵌套】(源目录整个搬进同名目录里再套一层), 不是文件级合并 ——
    会把整季塞进 '剧名/剧名/' 嵌套目录。所以补季必须逐文件搬:
    每个文件 MoveFile(文件 → 库内 Season N, conflict=Skip), 同名文件 Skip 保留库内那份。
    源目录【不改名】(改名成库内同名正是嵌套的诱因), 搬空后删除。
    """
    res = {"deleted": [], "renamed": None, "moved": None, "skipped": None,
           "cleaned_names": [], "renamed_media": {}, "nfo": None, "probed": False,
           "merged_seasons": plan.get("missing_seasons") or []}

    if plan.get("origin_filename") is None and (plan.get("media") or []):
        biggest = max(plan["media"], key=lambda f: f.get("size") or 0)
        plan["origin_filename"] = biggest.get("name") or ""

    if org_cfg(config).get("probe_media", True) and not plan.get("_probe_info"):
        plan["_probe_info"] = _probe_source_media(config, plan, base_dir=base_dir)

    if org_cfg(config).get("clean_media_names", True):
        cleaned = clean_media_names(config, plan, base_dir=base_dir, log=log)
        if cleaned:
            res["cleaned_names"] = cleaned
            log(f"    ✎  清理文件名推广块 {len(cleaned)} 个")

    trash = list(plan.get("ads") or []) + list(plan.get("junk") or [])
    if org_cfg(config).get("write_nfo", True) and (plan.get("meta") or {}).get("kind") == "tv":
        trash += list(plan.get("nfos") or [])
    if trash:
        cd2.delete_files(config, trash, base_dir=base_dir)
        res["deleted"] = trash
        log(f"    🗑  删除 {len(trash)} 个广告/杂项文件")

    existing = plan.get("existing")
    src = plan.get("source") or ""
    if not existing or not src:
        res["skipped"] = "缺少库内目标或源路径,跳过合并"
        log(f"    ⚠  {res['skipped']}")
        return res
    missing = set(plan.get("missing_seasons") or [])

    # 源目录全部文件(离线根下, 可自由读取/搬走)
    try:
        files = scan_tree(config, src, base_dir=base_dir)
    except Exception as e:  # noqa: BLE001
        res["skipped"] = f"读源目录失败: {str(e)[:120]}"
        log(f"    ⚠  {res['skipped']}")
        return res

    title = (plan.get("meta") or {}).get("title") or ""
    template = tv_file_template(config)
    q = quality_of_plan(config, plan) if rename_media_enabled(config) else ""

    def season_dir(season):
        key = ("sd", season)
        if key not in _MERGE_CACHE:
            _MERGE_CACHE[key] = cd2.ensure_folder(
                config, existing.rstrip("/"), naming.season_dir_name(season),
                base_dir=base_dir)
        return _MERGE_CACHE[key]

    def listed(season):
        key = ("ls", season)
        if key not in _MERGE_CACHE:
            try:
                _MERGE_CACHE[key] = {
                    (f.get("name") or "").lower()
                    for f in cd2.get_subfiles(config, _MERGE_CACHE[("sd", season)],
                                              base_dir=base_dir) if not cd2.is_dir(f)}
            except Exception:  # noqa: BLE001
                _MERGE_CACHE[key] = set()
        return _MERGE_CACHE[key]

    # ---- 第 1 遍: 视频 → 定新名(标准集名, 冲突则保留原名), 搬进库内季目录 ----
    vid_newname = {}      # 源文件名 → 新名(字幕跟随用)
    moved_any = False
    for f in files:
        name = f.get("name") or ""
        if naming.ext_of(name) not in naming.VIDEO_EXT:
            continue
        season, episode = naming.parse_episode(name)
        if season is None:
            season = naming.season_of_dirname(os.path.basename(f.get("rel_dir") or ""))
        if season is None or (missing and season not in missing):
            continue  # 不属于缺失季的绝不动(库内已有季不受影响)
        ext = naming.ext_of(name)
        new_name = name
        if q and episode is not None and title:
            cand = naming.tv_episode_filename(title, season, episode, q, template) + ext
            if cand != name:
                new_name = cand
        elif q and episode is None and naming._SEASON_ONLY_RE.search(name) \
                and not naming._SEASON_EP_RE.search(name):
            cand = naming.tv_episode_filename(title, season, None, q, template) + ext
            if cand != name:
                new_name = cand
        sd = season_dir(season)
        names = listed(season)
        if new_name.lower() in names:
            if name.lower() in names:
                continue  # 库内同名文件已在 → 跳过(不覆盖)
            new_name = name  # 标准名被占 → 保留原名再试
        if name.lower() in names:
            continue
        cd2.move_file(config, [f.get("path") or name], sd, conflict="Skip", base_dir=base_dir)
        cur = sd.rstrip("/") + "/" + name
        if new_name != name:
            try:
                cd2.rename_file(config, cur, new_name, base_dir=base_dir)
                cur = sd.rstrip("/") + "/" + new_name
            except Exception:  # noqa: BLE001
                pass
        names.add(new_name.lower())
        vid_newname[os.path.splitext(name)[0].lower()] = os.path.splitext(new_name)[0]
        res["renamed_media"][name] = cur
        moved_any = True
        log(f"    ➜  {name[:52]} → {os.path.basename(cur)[:56]}")

    # ---- 第 2 遍: 字幕跟随(同季 + 与已搬视频同主干 → 跟着改名) ----
    for f in files:
        name = f.get("name") or ""
        if naming.ext_of(name) not in naming.SUBTITLE_EXT:
            continue
        season, _ep = naming.parse_episode(name)
        if season is None:
            season = naming.season_of_dirname(os.path.basename(f.get("rel_dir") or ""))
        if season is None or (missing and season not in missing):
            continue
        stem, sext = os.path.splitext(name)
        new_stem = vid_newname.get(stem.lower())
        new_name = (new_stem + sext) if new_stem else name
        sd = season_dir(season)
        names = listed(season)
        if new_name.lower() != name.lower() and new_name.lower() in names:
            new_name = name
        if name.lower() in names:
            continue
        cd2.move_file(config, [f.get("path") or name], sd, conflict="Skip", base_dir=base_dir)
        cur = sd.rstrip("/") + "/" + name
        if new_name != name:
            try:
                cd2.rename_file(config, cur, new_name, base_dir=base_dir)
                cur = sd.rstrip("/") + "/" + new_name
            except Exception:  # noqa: BLE001
                pass
        names.add(new_name.lower())
        res["renamed_media"].setdefault(name, cur)
        moved_any = True

    res["moved"] = existing if moved_any else None
    if moved_any:
        log(f"    ⇄  并入已有剧目录 {existing}(补季: "
            + "、".join(naming.season_dir_name(s) for s in sorted(missing)) + ")")
    else:
        res["skipped"] = "没有可补的季文件(可能已在库内),源目录保留"
        log(f"    ⚠  {res['skipped']}")

    # 源目录搬空后删除(离线根下, 允许删除); 先清掉被搬空的旧季包子目录
    try:
        _prune_empty_dirs(config, src.rstrip("/"), base_dir=base_dir, log=log)
        left = cd2.get_subfiles(config, src.rstrip("/"), base_dir=base_dir)
        if not left and moved_any:
            cd2.delete_files(config, [src.rstrip("/")], base_dir=base_dir)
            log("    🗑  删除已搬空的源目录")
    except Exception:  # noqa: BLE001
        pass

    # 更新 tvshow.nfo(覆盖库内旧 NFO)
    meta = resolve_full_meta(config, plan)
    if meta and write_nfo_enabled(config):
        try:
            res["nfo"] = _write_entry_nfo(config, plan, existing.rstrip("/"), meta,
                                          plan.get("_probe_info"), base_dir=base_dir, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"    ⚠ NFO 写入失败: {str(e)[:150]}")
    _MERGE_CACHE.clear()
    return res


def quality_of_plan(config, plan):
    """取本条的质量标记: 优先搬运前探测结果,退回文件名推断。"""
    from lib import mediainfo
    info = plan.get("_probe_info")
    if info:
        try:
            return (mediainfo.quality_tag(info) or "").strip()
        except Exception:  # noqa: BLE001
            pass
    media = plan.get("media") or []
    biggest = max(media, key=lambda f: f.get("size") or 0) if media else {}
    return (mediainfo.quality_from_name(
        plan.get("origin_filename") or biggest.get("name") or "") or "").strip()


def preview_plan_files(config, plan, base_dir=None):
    """只读预览: 推算每个媒体文件整理后的【预期】名称与落点结构(不触发任何 CD2 写/删/搬)。

    真实执行见 apply_plan / finalize_entry(_reorganize_tv / _rename_media_for_movie);
    此处仅用同一套 naming 规则给出**可读预览**, 让用户执行前先看清"会改成什么、落到哪个结构"。
    质量标记: 复用 quality_of_plan(优先已探测结果, 否则文件名推断; 不主动触发 WebDAV 探测)。
    返回 dict:
        target/ new_name/ quality/ merge_existing
        media:[{src, dst, dst_path, note}]  —— 每个视频文件的预期结果
        deletes:[name]                       —— 广告/杂项(将删除, 走回收站)
        keeps:[name]                         —— 库内资产(保留)
    """
    res = {"target": plan.get("target"), "new_name": plan.get("new_name"),
           "quality": "", "media": [], "deletes": [], "keeps": [], "merge_existing": False}
    meta = plan.get("meta") or {}
    target = (plan.get("target") or "").rstrip("/")
    kind = meta.get("kind") or ""
    quality = quality_of_plan(config, plan) or ""
    res["quality"] = quality
    title = meta.get("title") or ""
    media = plan.get("media") or []
    tv_tpl = tv_file_template(config)
    mv_tpl = movie_file_template(config)
    res["merge_existing"] = plan.get("status") == "merge"

    for f in media:
        old = f.get("name") or ""
        ext = naming.ext_of(old)
        if kind == "tv":
            season, episode = naming.parse_episode(old)
            if season is None:
                season = _season_hint_from_dir(f.get("rel_dir"))
            if episode is None:
                episode = _episode_hint(old)
            if season is None:
                dst_dir, new, note = target, old, "季号不明 · 保留原名/原结构"
            else:
                sd = naming.season_dir_name(season)
                cand = naming.tv_episode_filename(title, season, episode, quality, tv_tpl) + ext
                dst_dir = (target + "/" + sd) if target else sd
                new = cand
                note = "" if cand != old else "保持原名"
        else:
            # 电影 / 未知: 单视频 → 标准电影名; 多视频或无元数据 → 保持原名
            if len(media) == 1 and title and naming.ext_of(old) in naming.VIDEO_EXT:
                cand = naming.render_template(mv_tpl, **naming.name_fields(meta, quality)) + ext
                new = cand
                note = "" if cand != old else "保持原名"
            else:
                new, note = old, "多视频/无元数据 · 保持原名"
            dst_dir = target
        dst_path = (dst_dir.rstrip("/") + "/" + new) if (new and dst_dir) else (new or old)
        # 相对落点(去掉 target 前缀): 电影=文件名, 剧集= Season N/文件名 —— 让结构可见
        rel = dst_path[len(target) + 1:] if (target and dst_path.startswith(target + "/")) else (new or old)
        res["media"].append({"src": old, "dst": new or old, "dst_path": dst_path,
                             "dst_rel": rel, "note": note})

    for a in (plan.get("ads") or []):
        res["deletes"].append(os.path.basename(a) if isinstance(a, str)
                              else (a.get("name") if isinstance(a, dict) else str(a)))
    for j in (plan.get("junk") or []):
        res["deletes"].append(os.path.basename(j) if isinstance(j, str)
                              else (j.get("name") if isinstance(j, dict) else str(j)))
    for a in (plan.get("assets") or []):
        res["keeps"].append(os.path.basename(a) if isinstance(a, str)
                            else (a.get("name") if isinstance(a, dict) else str(a)))
    return res


def apply_plan(config, plan, base_dir=None, log=print):
    """执行一个计划: 清文件名推广 → 删广告 → 改名 → 移动 → 探测/改名/写 NFO。"""
    res = {"deleted": [], "renamed": None, "moved": None, "skipped": None,
           "cleaned_names": [], "renamed_media": {}, "nfo": None, "probed": False}

    # 记下原始媒体文件名(写进 NFO 的 <original_filename>)
    if not plan.get("origin_filename") and (plan.get("media") or []):
        biggest = max(plan["media"], key=lambda f: f.get("size") or 0)
        plan["origin_filename"] = biggest.get("name") or ""

    # 0-pre) 媒体探测 —— **必须在搬运之前**。WebDAV 账号只开到 /Temp,
    #   搬到 /Cloud 后文件就探测不到了(没有本地挂载时 <fileinfo> 会丢失)。
    if org_cfg(config).get("probe_media", True) and not plan.get("_probe_info"):
        plan["_probe_info"] = _probe_source_media(config, plan, base_dir=base_dir)

    # 0) 媒体/字幕文件名去推广块(默认开,可用 organize.clean_media_names=false 关闭)
    if org_cfg(config).get("clean_media_names", True):
        cleaned = clean_media_names(config, plan, base_dir=base_dir, log=log)
        if cleaned:
            res["cleaned_names"] = cleaned
            log(f"    ✎  清理文件名推广块 {len(cleaned)} 个")
            for _old, new in cleaned[:3]:
                log(f"       → {new[:70]}")

    # 1) 删广告 + 杂项(默认进回收站,可恢复)
    trash = list(plan.get("ads") or []) + list(plan.get("junk") or [])
    # 即将写入我们自己的 NFO 时,连同旧的 .nfo 一起替换(避免同目录两个 NFO 打架);
    # 否则保留已有 NFO —— 不能抹掉别人的刮削结果。
    will_write_nfo = (write_nfo_enabled(config) and bool(plan.get("meta"))
                      and plan.get("status") == "ok")
    if will_write_nfo:
        trash += list(plan.get("nfos") or [])
    if trash:
        cd2.delete_files(config, trash, base_dir=base_dir)
        res["deleted"] = trash
        log(f"    🗑  删除 {len(trash)} 个广告/杂项文件")
    if plan.get("asset_files"):
        kept = "、".join(a["name"] for a in plan["asset_files"][:4])
        log(f"    ⛨  保留库内资产 {len(plan['asset_files'])} 个({kept}"
            + ("…" if len(plan["asset_files"]) > 4 else "") + ")")

    if plan.get("status") == "merge":
        # 剧集补季: 库内已有该剧目录(可能只含别的季) → 把本条的季【并入】库内目录。
        # 用 MoveFile 递归合并(同名文件 Skip,绝不覆盖),已有季的内容不受影响。
        return _apply_merge_seasons(config, plan, base_dir=base_dir, log=log)

    if plan.get("status") != "ok":
        return res

    new_name = plan.get("new_name")
    target_root = plan.get("target_root")
    if not new_name or not target_root:
        return res
    target_root = target_root.rstrip("/") or "/"
    target_path = target_root + "/" + new_name

    on_conflict = org_cfg(config).get("on_conflict", "skip")

    # 2) 目标已存在?(duplicate 已在 build_plans 拦下;这里兜底未判定的 ok 计划)
    existing = cd2.find_file_by_path(config, target_root, new_name, base_dir=base_dir)
    if existing and cd2.is_dir(existing) and plan.get("is_dir"):
        if on_conflict == "merge":
            # 先在源目录(离线工作区, 可删)内整理好 + 清掉搬空的旧目录, 再并入库内目录
            mv = _organize_in_workspace(config, plan, plan["source"], base_dir=base_dir, log=log)
            if mv:
                res["renamed_media"] = mv
            children = cd2.get_subfiles(config, plan["source"], base_dir=base_dir)
            if children:
                cd2.move_file(config, [c["fullPathName"] for c in children], target_path,
                              conflict="Skip", handle_conflict_recursively=True, base_dir=base_dir)
                log(f"    ⇄  并入已有目录 {target_path}")
            res["moved"] = target_path
            # 源目录如果空了就清掉(ClearOfflineFile 不适用,直接删空壳)
            try:
                left = cd2.get_subfiles(config, plan["source"], base_dir=base_dir)
                if not left:
                    cd2.delete_files(config, [plan["source"]], base_dir=base_dir)
                    log("    🗑  删除已搬空的源目录")
            except Exception:  # noqa: BLE001
                pass
            return res
        res["skipped"] = f"目标已存在,跳过: {target_path}"
        log(f"    ⚠  {res['skipped']}")
        return res

    src_parent = os.path.dirname(plan["source"].rstrip("/")) or "/"

    # 3) 目录: 先改名 → 在离线工作区(/Temp 离线下载目录, 可删)内整理好 → 再整体归位 /Cloud
    if plan.get("is_dir"):
        renamed = plan["source"]
        if plan["name"] != new_name:
            cd2.rename_file(config, plan["source"], new_name, base_dir=base_dir)
            renamed = src_parent + "/" + new_name
            res["renamed"] = renamed
            log(f"    ✎  改名 → {new_name}")
        # ★ 用户铁律: 先在 /Temp 离线目录里整理好, 再移到 /Cloud。
        #   /Cloud 禁删 → 剧集按季归位、电影改名、清空壳旧目录都必须留在可删的工作区做完,
        #   否则搬空的旧季包目录会永久残留在媒体库(清不掉)。
        mv = _organize_in_workspace(config, plan, renamed, base_dir=base_dir, log=log)
        if mv:
            res["renamed_media"] = mv
        if src_parent != target_root:
            cd2.move_file(config, renamed, target_root, base_dir=base_dir)
            res["moved"] = target_path
            log(f"    ➜  归位 {target_root}")
        else:
            res["moved"] = renamed
        res.update(finalize_entry(config, plan, target_path, resolve_full_meta(config, plan),
                                 base_dir=base_dir, log=log, skip_media_rename=True))
        return res

    # 4) 散落文件(没有自己的目录): 同样"先在 /Temp 工作区整理好再归位" ——
    #    在中转区(/Temp 内, 可删)建规范目录 → 文件+字幕收入其中 → 工作区内改名 → 再整体搬进 /Cloud。
    #    ⚠️ 不建在离线根: 若 new_name 与离线根里已有的条目目录撞名, ensure_folder 会复用它,
    #       随后整体 MoveFile 会把【别人还没整理的条目】一起搬进 /Cloud。
    stage = _ensure_path(config, cd2.staging_dir(config), base_dir=base_dir)
    work_name = new_name
    work_dir = cd2.ensure_folder(config, stage, work_name, base_dir=base_dir)
    if cd2.get_subfiles(config, work_dir, base_dir=base_dir):     # 中转区有同名残留 → 另起临时名
        work_name = f"{new_name}.__stage__{int(time.time())}"
        work_dir = cd2.ensure_folder(config, stage, work_name, base_dir=base_dir)
    payload = [plan["source"]] + list(plan.get("subtitles") or [])
    cd2.move_file(config, payload, work_dir, base_dir=base_dir)
    log(f"    ➜  收入工作目录 {work_name}")
    mv = _organize_in_workspace(config, plan, work_dir, base_dir=base_dir, log=log)
    if mv:
        res["renamed_media"] = mv
    if work_name != new_name:               # 临时名 → 规范名(仍在 /Temp 内, 未进 /Cloud)
        cd2.rename_file(config, work_dir, new_name, base_dir=base_dir)
        work_dir = stage.rstrip("/") + "/" + new_name
    if src_parent != target_root:
        cd2.move_file(config, work_dir, target_root, base_dir=base_dir)
        res["moved"] = target_path
        log(f"    ➜  归位 {target_path}")
    else:
        res["moved"] = work_dir
    res.update(finalize_entry(config, plan, target_path, resolve_full_meta(config, plan),
                             base_dir=base_dir, log=log, skip_media_rename=True))
    return res


def clean_unresolved_enabled(config):
    """反查不到/分类不出的条目,是否仍就地清掉广告与杂项(默认开)。

    这类条目不会被改名或移动(避免放错库),但广告文件是确定不要的,清掉是安全的。
    """
    return bool(org_cfg(config).get("clean_unresolved", True))


def run(config, base_dir=None, apply=False, only=None, limit=None, log=print):
    plans = build_plans(config, base_dir=base_dir, only=only, limit=limit)
    summary = {"total": len(plans), "ok": 0, "unresolved": 0, "no_media": 0,
               "duplicate": 0, "merge": 0, "applied": 0, "cleaned": 0,
               "dedup_cleaned": 0, "merged": 0,
               "deleted_files": 0, "cleaned_names": 0,
               "nfo_written": 0, "media_renamed": 0, "errors": []}

    for p in plans:
        tag = {"ok": "✓", "unresolved": "?", "no_media": "·", "duplicate": "≡",
               "merge": "⇄"}.get(p["status"], "?")
        log(f"\n{tag} {p['name']}")
        log(f"    媒体 {p['media_count']} 个 / {p['media_bytes'] / 1024 ** 3:.2f} GB"
            f"    广告杂项 {p['ad_count']} 个")
        if p["status"] == "ok":
            summary["ok"] += 1
            m = p.get("meta") or {}
            if m.get("title"):
                log(f"    元数据: {m['title']} ({m.get('year')}) "
                    f"imdb={m.get('imdb_id') or '-'} tmdb={m.get('tmdb_id')} "
                    f"[命中 {m.get('matched_query')!r} 匹配度 {m.get('match_score')}]")
            cat = p.get("category_folder")
            if cat:
                log(f"    分类: {cat} ({p.get('category')})  [{'/'.join(p.get('category_reasons') or [])}]")
            if p.get("new_name") and p.get("new_name") != p["name"]:
                log(f"    改名: {p['name'][:60]}")
                log(f"       →  {p['new_name']}")
            log(f"    → {p['target']}")
            if p["reason"] and not p.get("category_folder"):
                log(f"    {p['reason']}")
            if p["ad_count"]:
                for a in p["ad_files"][:5]:
                    log(f"      🗑 {a['name'][:70]}  ({a['size'] / 1024:.0f} KB)")
                if p["ad_count"] > len(p["ad_files"][:5]):
                    log(f"      … 另有 {p['ad_count'] - len(p['ad_files'][:5])} 个杂项")
        else:
            summary[p["status"]] = summary.get(p["status"], 0) + 1
            log(f"    {p['reason']}")
            if p["status"] == "merge":
                if p.get("new_name"):
                    log(f"    并入: {p.get('existing') or p.get('target')}"
                        f"(源目录改名 → {p['new_name']})")
            elif p["status"] == "duplicate":
                if p.get("target"):
                    log(f"    → (已在) {p['target']}")
                summary.setdefault("duplicate_list", []).append(
                    {"name": p["name"], "target": p.get("target")})
                for a in p["ad_files"][:5]:
                    log(f"      🗑 {a['name'][:70]}  ({a['size'] / 1024:.0f} KB)")

        do_apply = apply and (p["status"] in ("ok", "duplicate", "merge")
                             or (p["status"] == "unresolved" and clean_unresolved_enabled(config)))
        if do_apply:
            try:
                r = apply_plan(config, p, base_dir=base_dir, log=log)
                if p["status"] == "ok":
                    summary["applied"] += 1
                elif p["status"] == "duplicate":
                    summary["dedup_cleaned"] += 1
                elif p["status"] == "merge":
                    summary["merged"] += 1
                else:
                    summary["cleaned"] += 1
                summary["deleted_files"] += len(r.get("deleted") or [])
                summary["cleaned_names"] += len(r.get("cleaned_names") or [])
                if r.get("nfo"):
                    summary["nfo_written"] += 1
                if r.get("renamed_media"):
                    summary["media_renamed"] += 1
            except Exception as e:  # noqa: BLE001
                summary["errors"].append({"name": p["name"], "error": str(e)[:200]})
                log(f"    ✗ 执行失败: {str(e)[:200]}")

    return summary


def main():
    ap = argparse.ArgumentParser(description="离线目录整理: 清广告 → 改名 → 归位媒体库")
    ap.add_argument("--config", default=None, help="配置文件路径(默认 ./config.json)")
    ap.add_argument("--base-dir", default=None, help="项目根目录(默认本脚本上一级)")
    ap.add_argument("--apply", action="store_true", help="真正执行(默认只预览)")
    ap.add_argument("--only", default=None, help="只处理名字含该子串的条目")
    ap.add_argument("--limit", type=int, default=None, help="最多处理几个条目")
    ap.add_argument("--json", action="store_true", help="输出 JSON 计划(不打印人类可读日志)")
    args = ap.parse_args()

    base_dir = args.base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg_path = args.config or os.path.join(base_dir, "config.json")
    config = cd2.load_config(cfg_path)

    if args.json:
        plans = build_plans(config, base_dir=base_dir, only=args.only, limit=args.limit)
        print(json.dumps({"offline_root": offline_root(config), "plans": plans},
                         ensure_ascii=False, indent=2))
        return 0

    mode = "【执行】" if args.apply else "【预览 dry-run】"
    print(f"{mode} 离线根 {offline_root(config)}  →  媒体库 {cloud_root(config)}/<分类>")
    print(f"分类目录取自 config.categories,当前 "
          f"{len([k for k in (config.get('categories') or {}) if not k.startswith('_')])} 个")
    if not args.apply:
        print("(加 --apply 才会真正删除/改名/移动)")

    summary = run(config, base_dir=base_dir, apply=args.apply,
                  only=args.only, limit=args.limit, log=print)

    print(f"\n{'=' * 60}")
    print(f"共 {summary['total']} 个条目: 可整理 {summary['ok']} / "
          f"补季合并 {summary.get('merge', 0)} / "
          f"库中已有 {summary.get('duplicate', 0)} / "
          f"未匹配 {summary['unresolved']} / 无视频 {summary['no_media']}")
    if args.apply:
        print(f"已归位 {summary['applied']} 个,"
              f"补季并入 {summary.get('merged', 0)} 个,"
              f"已有跳过(仅清广告) {summary.get('dedup_cleaned', 0)} 个,"
              f"仅清理未归位 {summary.get('cleaned', 0)} 个,"
              f"删除广告杂项 {summary['deleted_files']} 个文件,"
              f"清理文件名 {summary['cleaned_names']} 个")
        print(f"写入 NFO {summary.get('nfo_written', 0)} 个,"
              f"视频改名 {summary.get('media_renamed', 0)} 个")
    if summary.get("duplicate"):
        dup = summary.get("duplicate_list") or []
        print(f"\n库里已有(未重复归位,源目录仍留在离线根,可自行删除):")
        for d in dup[:20]:
            print(f"  ≡ {d['name'][:56]} → {d.get('target')}")
        if len(dup) > 20:
            print(f"  … 另有 {len(dup) - 20} 个")
    if summary["errors"]:
        print(f"错误 {len(summary['errors'])} 个:")
        for e in summary["errors"]:
            print(f"  ✗ {e['name']}: {e['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
