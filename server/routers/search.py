"""磁力搜索路由: 按片名/剧集搜磁力

两个磁力源, 由配置的 enabled 开关决定用哪个(启用哪个用哪个):
  - 原生 Bitmagnet        (bitmagnet.enabled)          GraphQL, 带 seeders/leechers
  - Bitmagnet-Next-Web    (bitmagnet_next_web.enabled) REST(如 https://your-site.example.com), 通常更快, 无 seeders/leechers

选择规则:
  - 只启用一个   → 走那一个
  - 两个都启用   → 优先走更快的 bitmagnet_next_web(可配置 search.primary 覆盖)
  - 两个都禁用   → 503

只管搜磁力, 不做分类/TMDB 反查 —— 落库分类与 TMDB/IMDB 由 organize 阶段 本地 TMDB 缓存→TMDB 直连 完成,
不在搜索阶段凭磁力引擎的元数据(或文件名)猜。

排序(sort 参数):
  - relevance     引擎原序(默认), 按页直取最快, 不拉全量
  - quality       质量优先: 按种子名打分(2160p>HDR>H.265/AV1>Atmos>简繁英字幕>国语…),
                  高分在前, 同分按大小→种子数; 拉全量排序+120s 缓存
  - size_desc/size_asc  全局大小排序: 拉全量(上限 200 条)排序后按 limit 切片分页,
                        同查询+排序结果缓存 120s, 「加载更多」不重拉
  - seeders_desc  全局种子数排序; Next-Web 源无 seeders, 该源下退化为原序(缺失值排末尾)
"""
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from server.auth import require_auth
from server.config import get_config

router = APIRouter(prefix="/api/search", tags=["bitmagnet"], dependencies=[Depends(require_auth)])

# 两个源都启用时优先用哪个: "next_web"(默认, your-site.example.com 更快) 或 "native"
_DEFAULT_PRIMARY = "next_web"

# 排序模式: relevance=引擎原序(默认) | size_desc=大小从大到小 | size_asc=大小从小到大 | seeders_desc=种子从多到少
# 非 relevance 需要"全局排序", 即拉全量(带上限)排序后再切片分页, 否则只排一页没意义。
_SORT_MODES = {"relevance", "quality", "size_desc", "size_asc", "seeders_desc"}

# 中文字幕 / 国语 判定(单一来源: _QUALITY_RULES 与 is_golden/quality_score 共用, 避免两处漂移 ——
# 2026-09-19 实测教训: 两份正则分开维护, 只改了一处, "国语配音+中文字幕"种子漏金标)
# "中文字幕/中字/简繁" 都是发布组常见写法; 国配=国语配音
_ZH_SUB = re.compile(r"简繁|中英双|双语|双字|简中|繁中|中文|中字|简|繁")
_GUOYU = re.compile(r"国语|国配")

# 质量评分: 用户要求"2160p 往前排、H.265/x265 往前排、HDR 往前排、有字幕往前排、有国语往前排"。
# 每项独立加分(可叠加), 总分降序 = 质量优先。权重按"稀缺度"排:
# 分辨率 > HDR > 编码 > 音频 > 字幕 > 音轨语言。发布组原盘(BluRay)略优于 WEB(同源画质更稳)。
_QUALITY_RULES = [
    # (正则, 加分) — 全部在 name 大写后匹配
    (re.compile(r"2160P|UHD|4K|2160"), 48),        # 4K 分辨率
    (re.compile(r"HDR10\+|HDR10P"), 12),           # HDR10+
    (re.compile(r"HDR10|HDR"), 10),                # HDR
    (re.compile(r"AV1"), 12),                      # AV1(高效, 4K 主流)
    (re.compile(r"HEVC|H265|H\.?265|x265"), 9),    # H.265
    (re.compile(r"REMUX|BLU-?RAY|BDRIP|BDMV|HDMV"), 6),  # 原盘/高码率(裸 "BD" 子串太宽会误伤发布组名)
    (re.compile(r"ATMOS"), 6),                     # 杜比全景声
    (re.compile(r"DTS-?HD|TRUEHD|TRUE.?HD"), 5),   # 高解析音频
    (re.compile(r"DTS"), 3),
    (re.compile(r"1080P|1080I|FHD|1080"), 9),      # 1080p
    (re.compile(r"720P|720"), 3),                  # 720p
    (re.compile(r"1080I"), 0),
    (_ZH_SUB, 6),                                  # 中文字幕
    (re.compile(r"ENG|EN\b|英文"), 3),             # 英文字幕
    (_GUOYU, 6),                                   # 国语音轨
    (re.compile(r"原声"), 1),
    (re.compile(r"DVDRip|WEB-?DL|WEBRip|WEB"), 0), # 不加分(基准)
    (re.compile(r"480P|480|SD\b"), -8),            # 标清罚分
    (re.compile(r"SAMP|样本"), -20),               # 样片罚分
    (re.compile(r"10bit|10-BIT"), 2),
]


def golden_by(name, cfg):
    """种子名命中的"默认金标发布组"名(config `search.golden_groups`, 大小写不敏感); 无命中返回 None。

    用户自压的组(如 Linasi)放 DHT 的种子必然带中文字幕+国语, 但文件名里不一定写明
    (实测 `基督山伯爵.2024.UHD...Linasi` 没有"国语/中字"字样) → 按组名默认金标, 不靠文件名猜。
    """
    n = (name or "").upper()
    if not n:
        return None
    for g in ((cfg or {}).get("search") or {}).get("golden_groups") or []:
        g = (g or "").strip()
        if g and g.upper() in n:
            return g
    return None


def quality_score(name, cfg=None):
    """种子名质量分(越高越好)。2160p/HDR/H.265/字幕/国语 都有加分, 4K HDR H.265 国语种子会排最前。"""
    n = (name or "").upper()
    s = 0
    for pat, w in _QUALITY_RULES:
        if pat.search(n):
            s += w
    # 金标 → 额外 +20(用户 2026-09-19 要求"包含中文和国语的加金标、排序优先级更高")。
    # +20 的量级: 同档分辨率下金标稳定压过无字幕种子(战狼2 ★95→107 稳居第 1, 长津湖 ★90→110 第 1);
    # 4K 金标(≈107) > 4K 无字幕(≈79) > 1080p 金标(≈60), "下载即可看"在同画质下优先
    # 两种来源: ① 文件名双命中(中文字幕+国语); ② 命中 golden_groups 的自压组名(默认金标)
    if golden_by(name, cfg) or (_ZH_SUB.search(n) and _GUOYU.search(n)):
        s += 20
    return s


def is_golden(name, cfg=None):
    """金标种子: 同时带 中文字幕 与 国语音轨(下载后不用补字幕、直接国语可看),
    或属于 config `search.golden_groups` 里的自压发布组(默认金标, 不依赖文件名)。"""
    n = (name or "").upper()
    return bool(golden_by(name, cfg) or (_ZH_SUB.search(n) and _GUOYU.search(n)))
_SORT_CAP = 200          # 全局排序时最多拉取条数(防止热门词全量过大)
_SORT_CACHE_TTL = 120    # 全量排序结果缓存秒数(同一查询+排序, "加载更多"不重拉)
_sort_cache = {}         # key -> (ts, sorted_items)


def _sort_key(q, sort, source):
    return f"{source}|{sort}|{q.strip().lower()}"


def _cache_get(key):
    v = _sort_cache.get(key)
    if not v:
        return None
    ts, items = v
    if time.time() - ts > _SORT_CACHE_TTL:
        _sort_cache.pop(key, None)
        return None
    return items


def _cache_put(key, items):
    if len(_sort_cache) >= 64:
        _sort_cache.pop(next(iter(_sort_cache)), None)
    _sort_cache[key] = (time.time(), items)


def _apply_sort(items, sort, cfg=None):
    """就地排序(全局)。relevance 不动; 缺失值的排到末尾, 不报错。"""
    if sort == "quality":
        # 质量分降序; 同分按大小降序(同分辨率里大文件=更高码率/Remux 更优);
        # 再同分按 seeders(Next-Web 源无 seeders → 缺失排末尾, 同值保持原序)
        items.sort(key=lambda r: (
            quality_score(r.get("name"), cfg),
            r.get("size") or 0,
            r.get("seeders") is not None, r.get("seeders") or 0,
        ), reverse=True)
    elif sort == "size_desc":
        items.sort(key=lambda r: (r.get("size") or 0), reverse=True)
    elif sort == "size_asc":
        items.sort(key=lambda r: (r.get("size") or 0))
    elif sort == "seeders_desc":
        # 有 seeders 的按值降序排前, seeders=None(Next-Web 源) 的排最后
        items.sort(key=lambda r: (r.get("seeders") is not None, r.get("seeders") or 0), reverse=True)
    return items


# ---------------------------------------------------------------------------
# 推送标记: 给每个磁力附上"是否已推 CD2 / Qbit"(前端据此禁用按钮 + 显示徽章)
# ---------------------------------------------------------------------------
def _annotate_pushed(items):
    """就地给 items 追加 pushed:{cd2,qbit}。无 hash / 查库失败则默认全 False。"""
    hashes = [(r.get("infoHash") or "").lower() for r in items if r.get("infoHash")]
    if not hashes:
        for r in items:
            r.setdefault("pushed", {"cd2": False, "qbit": False})
        return
    try:
        from db.database import SessionLocal
        from db import repositories as repo
        s = SessionLocal()
        try:
            states = repo.get_push_states(s, hashes)
        finally:
            s.close()
    except Exception:  # noqa: BLE001  标记查询失败不影响磁力列表主流程
        states = {}
    for r in items:
        h = (r.get("infoHash") or "").lower()
        r["pushed"] = states.get(h) or {"cd2": False, "qbit": False}


def _enabled(cfg, key):
    """某源是否启用: 配置段缺失(未配置)视为关; 段在但没写 enabled 视为开; 否则取 enabled 值。"""
    sec = cfg.get(key)
    if not sec:
        return False
    return bool(sec.get("enabled", True))


def _pick_source(cfg):
    """返回应使用的源: 'native' | 'next_web' | None(都没启用)。"""
    native_on = _enabled(cfg, "bitmagnet")
    nw_on = _enabled(cfg, "bitmagnet_next_web")
    if not native_on and not nw_on:
        return None
    if native_on and nw_on:
        return (cfg.get("search") or {}).get("primary", _DEFAULT_PRIMARY) or _DEFAULT_PRIMARY
    return "native" if native_on else "next_web"


async def _search_native(cfg, q, limit):
    from scripts import search as bm_search
    results = await run_in_threadpool(bm_search.search, cfg, q, limit)
    out = []
    for r in results:
        magnet = r.get("magnetLink") or (
            f"magnet:?xt=urn:btih:{r['infoHash']}" if r.get("infoHash") else "")
        out.append({
            "infoHash": r.get("infoHash"),
            "name": r.get("name"),
            "size": r.get("size"),
            "seeders": r.get("seeders"),
            "leechers": r.get("leechers"),
            "magnet": magnet,
        })
    # 原生 GraphQL 不支持 page 翻页(limit 即上限)
    return out, False, 1


async def _search_next_web(cfg, q, limit, page=1):
    from scripts import diao_search
    base, _ = diao_search.resolve_settings(cfg)  # base 取自 config, 条数由 want 控制
    # 站点单页硬上限 10 条(limit>10 直接 400), collect 内部按 offset 自动翻页;
    # page 按 10 条/页折算成起始 offset, 支持"加载更多"续翻。
    start_offset = max(0, page - 1) * diao_search.PAGE_SIZE
    items, _total, _kw, more, end_offset = await run_in_threadpool(
        diao_search.collect, base, q, max(1, limit), start_offset=start_offset)
    out = []
    for t in items:
        out.append({
            "infoHash": t.get("hash"),
            "name": t.get("name"),
            "size": t.get("size"),
            "seeders": None,   # Next-Web 源不提供 seeders/leechers
            "leechers": None,
            "magnet": t.get("magnet"),
        })
    # 精确续翻页号: 实际翻到的 offset 换算回 page(去重可能少走页, 不能按返回条数猜)
    next_page = end_offset // diao_search.PAGE_SIZE + 1
    return out, bool(more), next_page


async def _fetch_all(source, cfg, q, cap):
    """全局排序用: 拉全量(最多 cap 条)并规整成统一 item 结构。"""
    if source == "next_web":
        from scripts import diao_search
        base, _ = diao_search.resolve_settings(cfg)
        # collect 内部按 offset 翻页, want=cap 一次拿全; 受 MAX_PAGES(200) 上限约束
        items, _tc, _kw, _more, _end = await run_in_threadpool(
            diao_search.collect, base, q, min(cap, 200), start_offset=0)
        out = []
        for t in items:
            out.append({
                "infoHash": t.get("hash"), "name": t.get("name"), "size": t.get("size"),
                "seeders": None, "leechers": None, "magnet": t.get("magnet"),
            })
        return out
    else:
        from scripts import search as bm_search
        results = await run_in_threadpool(bm_search.search, cfg, q, min(cap, 100))
        out = []
        for r in results:
            magnet = r.get("magnetLink") or (
                f"magnet:?xt=urn:btih:{r['infoHash']}" if r.get("infoHash") else "")
            out.append({
                "infoHash": r.get("infoHash"), "name": r.get("name"), "size": r.get("size"),
                "seeders": r.get("seeders"), "leechers": r.get("leechers"), "magnet": magnet,
            })
        return out


@router.get("")
async def api_search(q: str = Query(..., min_length=1), limit: int = 20,
                     page: int = Query(1, ge=1), sort: str = "relevance",
                     cfg: dict = Depends(get_config)):
    if not q.strip():
        raise HTTPException(400, "查询词不能为空")
    limit = max(1, min(int(limit or 20), 100))
    if sort not in _SORT_MODES:
        sort = "relevance"

    source = _pick_source(cfg)
    if source is None:
        raise HTTPException(503, "两个磁力源都已禁用(bitmagnet.enabled 与 bitmagnet_next_web.enabled)")

    # ---- 非 relevance: 拉全量 → 全局排序 → 按 limit 切片分页 ----
    if sort != "relevance":
        key = _sort_key(q, sort, source)
        all_items = _cache_get(key)
        try:
            if all_items is None:
                all_items = _apply_sort(await _fetch_all(source, cfg, q, _SORT_CAP), sort)
                _cache_put(key, all_items)
        except Exception as e:  # noqa: BLE001
            label = "Bitmagnet-Next-Web" if source == "next_web" else "Bitmagnet"
            raise HTTPException(status_code=502, detail=f"{label} 请求失败: {e}")

        start = (page - 1) * limit
        page_items = all_items[start:start + limit]
        has_more = start + limit < len(all_items)
        if sort == "quality":
            # 质量分+金标回传前端: 双查询合并后按同规则重排, 金标徽章渲染
            for it in page_items:
                it["qualityScore"] = quality_score(it.get("name"), cfg)
                it["golden"] = is_golden(it.get("name"), cfg)
                it["goldenBy"] = golden_by(it.get("name"), cfg)  # 命中自压组名 → 前端标注"自压"
        _annotate_pushed(page_items)
        return {
            "source": source, "items": page_items, "hasMore": has_more,
            "nextPage": page + 1 if has_more else page, "sort": sort,
            "totalCount": len(all_items),
        }

    # ---- relevance: 引擎原序, 按页直取(不拉全量, 最快) ----
    try:
        if source == "next_web":
            items, has_more, next_page = await _search_next_web(cfg, q, limit, page=page)
        else:
            items, has_more, next_page = await _search_native(cfg, q, limit)
    except Exception as e:  # noqa: BLE001
        label = "Bitmagnet-Next-Web" if source == "next_web" else "Bitmagnet"
        raise HTTPException(status_code=502, detail=f"{label} 请求失败: {e}")

    _annotate_pushed(items)
    return {"source": source, "items": items, "hasMore": has_more, "nextPage": next_page, "sort": sort}
