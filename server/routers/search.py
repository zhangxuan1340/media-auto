"""磁力搜索路由: 按片名/剧集搜磁力

两个磁力源, 由配置的 enabled 开关决定用哪个(启用哪个用哪个):
  - 原生 Bitmagnet        (bitmagnet.enabled)          GraphQL, 带 seeders/leechers
  - Bitmagnet-Next-Web    (bitmagnet_next_web.enabled) REST(改版站), 通常更快, 无 seeders/leechers

选择规则:
  - 只启用一个   → 走那一个
  - 两个都启用   → 优先走更快的 bitmagnet_next_web(可配置 search.primary 覆盖)
  - 两个都禁用   → 503

只管搜磁力, 不做分类/TMDB 反查 —— 落库分类与 TMDB/IMDB 由 organize 阶段 本地 TMDB 缓存→TMDB 直连 完成,
不在搜索阶段凭磁力引擎的元数据(或文件名)猜。

排序(sort 参数):
  - relevance     引擎原序(默认), 按页直取最快, 不拉全量
  - quality       质量优先: **前排发布组(种子抓取规则 search.group_priority)整批排最前**
                  (组序=配置顺序), 组内/其余再按质量分(2160p>HDR>H.265/AV1>Atmos>简繁英字幕>国语…),
                  同分按大小→种子数; 分段窗口排序 + 120s 缓存(改配置立即换 key 重排)
  - size_desc/size_asc  全局大小排序: 分段窗口排序后按 limit 切片分页(上限 200 条),
                        同查询+排序结果缓存 120s, 「加载更多」不重拉
  - seeders_desc  全局种子数排序; Next-Web 源无 seeders, 该源下退化为原序(缺失值排末尾)

非 relevance 的"全量"是**分段窗口**(2026-09-30): 首屏只抓 max(need+30, 60) 条, 「加载更多」
要更多时再重抓更大的窗口(到 _SORT_CAP=200 封顶), 而不是每次请求都拉满 200 条 —— 站点每页
10 条要 2~3.5s, 老实现首屏串行翻 20 页要 12~20s, 缓存 120s 过期后点一次「加载更多」又是十几秒,
表现就是"详情页很慢、加载更多点了没反应"。抓取本身在 scripts/diao_search.collect 里并行翻页。
"""
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from server.auth import require_auth
from server.config import get_config

router = APIRouter(prefix="/api/search", tags=["bitmagnet"], dependencies=[Depends(require_auth)])

# 两个源都启用时优先用哪个: "next_web"(默认, 通常更快) 或 "native"
_DEFAULT_PRIMARY = "next_web"

# 排序模式: relevance=引擎原序(默认) | size_desc=大小从大到小 | size_asc=大小从小到大 | seeders_desc=种子从多到少
# 非 relevance 需要"全局排序", 即抓一个分段窗口(带上限)排序后再切片分页, 否则只排一页没意义。
_SORT_MODES = {"relevance", "quality", "size_desc", "size_asc", "seeders_desc"}

# 中文字幕 / 国语 判定(单一来源: _QUALITY_RULES 与 is_golden/quality_score 共用, 避免两处漂移 ——
# 2026-09-19 实测教训: 两份正则分开维护, 只改了一处, "国语配音+中文字幕"种子漏金标)
# "中文字幕/中字/简繁" 都是发布组常见写法; 国配=国语配音
_ZH_SUB = re.compile(r"简繁|中英双|双语|双字|简中|繁中|中文|中字|简|繁")
_GUOYU = re.compile(r"国语|国配")

# 质量评分: 用户要求"2160p 往前排、H.265/x265 往前排、HDR 往前排、有字幕往前排、有国语往前排"。
# 每项独立加分(可叠加), 总分降序 = 质量优先。权重按"稀缺度"排:
# 分辨率 > HDR > 编码 > 音频 > 字幕 > 音轨语言。发布组原盘(BluRay)略优于 WEB(同源画质更稳)。
# 分辨率与体积另算(见 resolution()/_size_adjust()): 分辨率要"名副其实", 体积要"撑得住"。
_QUALITY_RULES = [
    # (正则, 加分) — 全部在 name 大写后匹配(分辨率不在这里, 单独判定)
    (re.compile(r"HDR10\+|HDR10P"), 12),           # HDR10+
    (re.compile(r"HDR10|HDR"), 10),                # HDR
    (re.compile(r"AV1"), 12),                      # AV1(高效, 4K 主流)
    (re.compile(r"HEVC|H265|H\.?265|x265"), 9),    # H.265
    (re.compile(r"REMUX|BLU-?RAY|BDRIP|BDMV|HDMV"), 6),  # 原盘/高码率(裸 "BD" 子串太宽会误伤发布组名)
    (re.compile(r"ATMOS"), 6),                     # 杜比全景声
    (re.compile(r"DTS-?HD|TRUEHD|TRUE.?HD"), 5),   # 高解析音频
    (re.compile(r"DTS"), 3),
    (_ZH_SUB, 6),                                  # 中文字幕
    (re.compile(r"ENG|EN\b|英文"), 3),             # 英文字幕
    (_GUOYU, 6),                                   # 国语音轨
    (re.compile(r"原声"), 1),
    (re.compile(r"DVDRip|WEB-?DL|WEBRip|WEB"), 0), # 不加分(基准)
    (re.compile(r"SAMP|样本"), -20),               # 样片罚分
    (re.compile(r"10bit|10-BIT"), 2),
]

# ---------------------------------------------------------------------------
# 分辨率判定: 2026-09-30 报障「明明很多低质量的变成了高质量」——
#   老实现按 `2160P|UHD|4K|2160` 一律 +48, 像《痴迷(2026)【4K.SDR1080p】》这种
#   "4K 压制的 1080p" 也拿满 48 分, 把真 1080p 蓝光压在下面。
#   现在: 出现 2160P/3840x2160 → uhd; 只出现 4K/UHD 但同时写了 1080 → 按 1080 算。
# ---------------------------------------------------------------------------
_RES_UHD = re.compile(r"2160[PU]?|3840X?2160")
_RES_4K = re.compile(r"4K|UHD")
_RES_1080 = re.compile(r"1080[PI]|FHD|1080")
_RES_720 = re.compile(r"720[PI]?|720")
_RES_SD = re.compile(r"480[PU]?|480|SD\b")
_RES_W = {"uhd": 48, "fhd": 9, "hd": 3, "sd": -8}   # 与老 _QUALITY_RULES 里的分辨率权重一致


def resolution(name):
    """分辨率档: 'uhd'(2160p/4K)/'fhd'(1080p)/'hd'(720p)/'sd'(480p)/None(名字没写)。"""
    n = (name or "").upper()
    if not n:
        return None
    if _RES_UHD.search(n) or (_RES_4K.search(n) and not _RES_1080.search(n)):
        return "uhd"
    if _RES_1080.search(n):
        return "fhd"
    if _RES_720.search(n):
        return "hd"
    if _RES_SD.search(n):
        return "sd"
    return None


def _size_adjust(res, size):
    """体积合理性(缺 size 不生效)。

    同分辨率按"一部两小时片该有多大"给分:真原盘/REMUX 再加, 名为 4K 却只有几十 MB
    的(实测《痴迷(2026)》一批 54~70MB 的 "4K/REMUX")基本是短片、拼接或诱饵,
    与真 4K 同分排前面就是"低质量被当成高质量"。
    """
    if not res or not size or size <= 0:
        return 0
    mb = size / 1048576
    if mb < 1:
        # <1MiB: 站点的占位/未知值(实测有的条目只回 ~100KB), 当体积样本没意义,
        # 一律按"体积未知"处理 —— 免得把真种子误判成"名不副实"。
        return 0
    if res == "uhd":
        if mb >= 30_000: return 10   # ≥30G 真原盘/REMUX
        if mb >= 10_000: return 6    # ≥10G 正常 4K 压制/WEB
        if mb >= 4_000: return 0
        if mb >= 1_000: return -45   # 1~4G: 两小时 4K 撑不到这个码率(多为升频/拼接)
        if mb >= 100: return -60     # 100M~1G: 必是短片/样本/诱饵
        return -75                   # <100M
    if res == "fhd":
        if mb >= 6_000: return 6
        if mb >= 1_500: return 0
        if mb >= 400: return -15
        return -35
    if res == "hd":
        if mb >= 600: return 3
        if mb >= 150: return 0
        return -20
    return 0


# 扣到这个量级就算"名不副实", 前端打「体积可疑」标签(与 _size_adjust 同口径)
_SIZE_SUSPECT_AT = -35


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


def quality_score(name, cfg=None, size=None):
    """种子名质量分(越高越好)。2160p/HDR/H.265/字幕/国语 都有加分, 4K HDR H.265 国语种子会排最前。

    size 给了就叠加体积合理性(_size_adjust): 名不副实的"4K"(几十 MB)会掉到 1080p 之下。
    """
    n = (name or "").upper()
    res = resolution(n)
    s = _RES_W.get(res, 0)
    for pat, w in _QUALITY_RULES:
        if pat.search(n):
            s += w
    s += _size_adjust(res, size)
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


# ---------------------------------------------------------------------------
# 前排发布组(种子抓取规则): 用户 2026-09-28 要求 FRDS / Beitai / 各大 PT 站的组
# "前排"—— 详情页「质量优先」搜索与追踪自动推送都先选它们。
# 配置 `search.group_priority`(顺序 = 优先级, 一项一个组名, 大小写不敏感):
#   - 键不存在(老库) → 用下面的默认列表;
#   - 显式空数组      → 关闭前排规则。
# ---------------------------------------------------------------------------
DEFAULT_GROUP_PRIORITY = ["FRDS", "Beitai", "HHD", "CHD", "OurBits", "Pter",
                          "MTeam", "TTG", "SSD", "HDChina", "DreamHD", "CHDBits", "Wiki"]


def group_priority_list(cfg):
    """前排组列表: 按配置顺序, 去空白、大小写不敏感去重(保留首次出现的写法)。"""
    sec = (cfg or {}).get("search") or {}
    raw = sec["group_priority"] if "group_priority" in sec else DEFAULT_GROUP_PRIORITY
    if isinstance(raw, str):
        raw = [raw]
    out, seen = [], set()
    for g in raw or []:
        g = str(g or "").strip()
        k = g.upper()
        if g and k not in seen:
            seen.add(k)
            out.append(g)
    return out


_grp_re_cache = {}


def _group_re(g):
    """组名匹配式: 两侧不紧挨字母/数字 —— `CHDRip` 不算组 `CHD`、`SSDX` 不算 `SSD`,
    而 `-Beitai` / `[FRDS]` / ` FRDS ` 都能中(组名先转大写再比, 大小写不敏感)。"""
    k = g.upper()
    rx = _grp_re_cache.get(k)
    if rx is None:
        rx = re.compile(r"(?<![A-Z0-9])" + re.escape(k) + r"(?![A-Z0-9])")
        _grp_re_cache[k] = rx
    return rx


def group_rank(name, cfg):
    """种子名命中的前排组序号(0 = 最靠前), 没命中返回 None。"""
    n = (name or "").upper()
    if not n:
        return None
    for i, g in enumerate(group_priority_list(cfg)):
        if _group_re(g).search(n):
            return i
    return None


_SORT_CAP = 200          # 全局排序时最多拉取条数(防止热门词全量过大)
_SORT_CACHE_TTL = 120    # 全量排序结果缓存秒数(同一查询+排序, "加载更多"不重拉)
_WINDOW0 = 60            # 首屏窗口下限(至少抓这么多条, 否则一次只排一屏没意义)
_STEP = 30               # 每页续抓增量(need+30 → 需要 90 条时抓 120)
_sort_cache = {}         # key -> (ts, sorted_items, fetched_cap, exhausted)


def _rule_fingerprint(cfg):
    """排序规则指纹(前排组 + 金标组): 改了配置就换 key, 不用等 120s 缓存过期。"""
    sec = (cfg or {}).get("search") or {}
    golden = [str(g or "").strip().lower() for g in (sec.get("golden_groups") or [])]
    return "|".join(g.lower() for g in group_priority_list(cfg)) + "#" + ",".join(golden)


def _sort_key(q, sort, source, cfg=None):
    return f"{source}|{sort}|{q.strip().lower()}|{_rule_fingerprint(cfg)}"


def _cache_get(key):
    """命中返回 (items, fetched_cap, exhausted), 过期/未命中返回 None。"""
    v = _sort_cache.get(key)
    if not v:
        return None
    ts, items, cap, exhausted = v
    if time.time() - ts > _SORT_CACHE_TTL:
        _sort_cache.pop(key, None)
        return None
    return items, cap, exhausted


def _cache_put(key, items, cap=None, exhausted=None):
    """cap = 这份 items 对应抓了多少条(用于判断要不要再抓更多); exhausted 缺省视为"已到底"。"""
    if len(_sort_cache) >= 64:
        _sort_cache.pop(next(iter(_sort_cache)), None)
    _sort_cache[key] = (time.time(), items,
                        len(items) if cap is None else cap,
                        True if exhausted is None else exhausted)


async def _load_window(source, cfg, q, need, sort):
    """拿一个"至少 need 条"的全局排序窗口(分段抓取), 返回 (sorted_items, exhausted)。

    exhausted=True 表示站点已到底(再翻也不会有新结果)。
    分段的意义: 老实现每个请求都拉满 _SORT_CAP=200(站点每页 10 条, 串行翻 20 页 = 12~20s),
    首屏要等很久; 缓存 120s 过期后点一次「加载更多」又是十几秒 —— 2026-09-30 报的
    「详情页种子很慢 / 加载更多点了没反应」就出在这里。
    """
    need = max(1, int(need))
    key = _sort_key(q, sort, source, cfg)
    hit = _cache_get(key)
    if hit:
        items, cap_used, exhausted = hit
        if len(items) >= need or exhausted or cap_used >= _SORT_CAP:
            return items, exhausted
    target = min(_SORT_CAP, max(need + _STEP, _WINDOW0))
    got = await _fetch_all(source, cfg, q, target)
    items = _apply_sort(got, sort, cfg)
    # 站点一条不剩(本页不满) → 到底; 满 target 条则认为后面还有, 下次要更多再抓
    exhausted = len(got) < target
    _cache_put(key, items, target, exhausted)
    return items, exhausted


def _apply_sort(items, sort, cfg=None):
    """就地排序(全局)。relevance 不动; 缺失值的排到末尾, 不报错。"""
    if sort == "quality":
        # 前排组(种子抓取规则)先按配置顺序整批排最前, 组内/其余再比质量:
        # 质量分降序 → 同分按大小降序(同分辨率里大文件=更高码率/Remux 更优)
        # → 再同分按 seeders(Next-Web 源无 seeders → 缺失排末尾, 同值保持原序)
        items.sort(key=lambda r: (
            _group_rank_key(r.get("name"), cfg),
            quality_score(r.get("name"), cfg, r.get("size")),
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


def _group_rank_key(name, cfg):
    """排序用的前排组键: (是否前排, 序号取负) —— reverse=True 下越大越靠前,
    所以 (1, 0) 的 FRDS 排在 (1, -1) 的 Beitai 前, 非前排组统一 (0, 0) 垫底。"""
    pr = group_rank(name, cfg)
    if pr is None:
        return (0, 0)
    return (1, -pr)


def _annotate_group(items, cfg):
    """就地给 items 追加 groupRank(前排组序号, 未命中 None) / groupName(命中的组名),
    供前端「前排」徽章与双查询合并后的同规则重排使用。"""
    names = group_priority_list(cfg)
    for it in items:
        pr = group_rank(it.get("name"), cfg)
        it["groupRank"] = pr
        it["groupName"] = names[pr] if pr is not None else None
    return items


def _annotate_quality(items, cfg):
    """就地回传质量分/金标/前排组: 双查询合并后前端按同规则重排, 徽章渲染。"""
    for it in items:
        it["qualityScore"] = quality_score(it.get("name"), cfg, it.get("size"))
        it["golden"] = is_golden(it.get("name"), cfg)
        it["goldenBy"] = golden_by(it.get("name"), cfg)  # 命中自压组名 → 前端标注"自压"
    return _annotate_group(items, cfg)


# ---------------------------------------------------------------------------
# 推送标记: 给每个磁力附上"是否已推 CD2 / Qbit"(前端据此禁用按钮 + 显示徽章)
# ---------------------------------------------------------------------------
def _annotate_suspect(items):
    """就地标记 sizeSuspect: 名不副实的体积(如 4K 只有几十 MB)。

    与质量分里的 _size_adjust 同口径(扣到 _SIZE_SUSPECT_AT 即视为可疑), 前端据此给「体积可疑」标签,
    让"名字吹 4K、体积几十 MB"的种子一眼看出来, 而不是被质量分抬到前面。
    """
    for it in items:
        it["sizeSuspect"] = _size_adjust(resolution(it.get("name")), it.get("size")) <= _SIZE_SUSPECT_AT
    return items


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
    """抓最多 cap 条并规整成统一 item 结构(全局排序的取数原语, 也是 track_check 的入口)。

    顺序抓取由 scripts/diao_search.collect 内部并行化; 返回不足 cap 条 = 站点到底。
    """
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


# ---------------------------------------------------------------------------
# 协议探测(管理 → 通用 → 磁力搜索源): 输入地址后自动试 https / http
# ---------------------------------------------------------------------------
_PROBE_TIMEOUT = 8.0


async def _probe_one(url: str, kind: str):
    """探一个完整地址: 只要收到 HTTP 响应就算通(4xx 也说明协议与端口是通的)。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, trust_env=False, verify=False,
                                     follow_redirects=True) as c:
            if kind == "graphql":
                # Bitmagnet 地址本身就是端点(如 http://host:3333/graphql), POST 最小查询
                r = await c.post(url, json={"query": "{__typename}"},
                                 headers={"Accept": "application/json", "User-Agent": "media-auto"})
            else:
                # 改版站按约定提供 GET {base}/api/stats
                r = await c.get(url.rstrip("/") + "/api/stats",
                                headers={"Accept": "application/json", "User-Agent": "media-auto"})
        return True, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, (str(e).strip() or e.__class__.__name__)[:140]


@router.get("/probe")
async def api_probe(url: str = Query(..., min_length=1), kind: str = Query("rest")):
    """按给定地址逐个协议试, 返回第一个连通的完整地址。

    url 可带可不带协议头: 带则先试它、再试另一个; 不带按 https → http 顺序试。
    kind: `rest` = 改版站 `{base}/api/stats`; `graphql` = Bitmagnet 端点,
    地址没写路径时自动补 `/graphql`。
    """
    if kind not in ("rest", "graphql"):
        kind = "rest"
    raw = (url or "").strip()
    m = re.match(r"^(?P<scheme>https?)://(?P<rest>.+)$", raw, re.IGNORECASE)
    rest = (m.group("rest") if m else raw).strip().rstrip("/")
    if not rest:
        raise HTTPException(400, "地址不能为空")
    if kind == "graphql" and "/" not in rest:
        rest = rest + "/graphql"      # 只给了 host[:port] → 补端点路径
    first = m.group("scheme").lower() if m else None
    schemes = ([first] if first else []) + [s for s in ("https", "http") if s != first]
    tried = []
    for scheme in schemes:
        full = f"{scheme}://{rest}"
        ok, detail = await _probe_one(full, kind)
        tried.append({"url": full, "ok": ok, "detail": detail})
        if ok:
            return {"ok": True, "url": full, "scheme": scheme, "tried": tried}
    return {"ok": False, "url": "", "scheme": None, "tried": tried}


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

    # ---- 非 relevance: 分段抓取 → 全局排序 → 按 limit 切片分页 ----
    if sort != "relevance":
        try:
            # 只抓到"本页够用 + 一点余量"(首屏 60, 续翻按需 +30 递增, 上限 200)
            window, exhausted = await _load_window(source, cfg, q, page * limit, sort)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            label = "Bitmagnet-Next-Web" if source == "next_web" else "Bitmagnet"
            raise HTTPException(status_code=502, detail=f"{label} 请求失败: {e}")

        start = (page - 1) * limit
        page_items = window[start:start + limit]
        # 还有下一页: 窗口里还有没展示的, 或窗口没抓满且站点未必到底(下次抓更大窗口)
        has_more = ((start + limit < len(window))
                    or (not exhausted and len(window) < _SORT_CAP))
        # 回传前端: 双查询合并后按同规则重排, 前排/金标徽章渲染
        if sort == "quality":
            _annotate_quality(page_items, cfg)
        else:
            _annotate_group(page_items, cfg)
        _annotate_suspect(page_items)
        _annotate_pushed(page_items)
        return {
            "source": source, "items": page_items, "hasMore": has_more,
            "nextPage": page + 1 if has_more else page, "sort": sort,
            "totalCount": len(window), "exhausted": exhausted,
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

    _annotate_group(items, cfg)   # 前排徽章在相关性排序下也照常显示
    _annotate_suspect(items)
    _annotate_pushed(items)
    return {"source": source, "items": items, "hasMore": has_more, "nextPage": next_page, "sort": sort}
