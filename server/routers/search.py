"""磁力搜索路由: 按片名/剧集搜磁力

三个磁力源, 各自由配置的 enabled 开关独立启停, 可单开一个, 也可同时开多个:
  - 原生 Bitmagnet        (bitmagnet.enabled)          GraphQL, 带 seeders/leechers
  - Bitmagnet-Next-Web    (bitmagnet_next_web.enabled) REST(改版站), 通常更快, 无 seeders/leechers
  - Jackett               (jackett.enabled)            Torznab, 聚合 Jackett 里配置的所有站, 带 seeders

多源规则:
  - 启用若干源   → 并行查所有启用的源, 按 infoHash 合并去重, 每条带 source 来源;
                  某源查询失败不影响其它源(全部失败才 502; 部分失败用成功的并回 warnings)
  - 全部禁用     → 503

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
import asyncio
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from server.auth import require_auth
from server.config import get_config

router = APIRouter(prefix="/api/search", tags=["bitmagnet"], dependencies=[Depends(require_auth)])

# 源标识固定顺序(多源合并时的展示顺序 / 错误信息都用它): native → next_web → jackett
_SOURCE_ORDER = ("native", "next_web", "jackett")

# 排序模式: relevance=引擎原序(默认) | size_desc=大小从大到小 | size_asc=大小从小到大 | seeders_desc=种子从多到少
# 非 relevance 需要"全局排序", 即抓一个分段窗口(带上限)排序后再切片分页, 否则只排一页没意义。
_SORT_MODES = {"relevance", "quality", "size_desc", "size_asc", "seeders_desc"}

# Bitmagnet ContentType 枚举(新版 schema 小写值), 前端按详情页类型传 movie/tv_show 消噪
_CTYPE_WHITELIST = {"movie", "tv_show", "music", "ebook", "comic", "audiobook", "game", "software", "xxx"}

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
    # 降权组(2026-10-04 用户要求): BTM/俄语组等无中文字幕的片源 → -20, 与金标 +20 对称,
    # 把这类种子压到后段(同画质下让位于带中文字幕/国语的片源)。组名见 config `search.group_demote`。
    if demote_by(name, cfg):
        s -= 20
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


# ---------------------------------------------------------------------------
# 降权发布组(2026-10-04 用户要求): 某些组(如 BTM / 一批俄语组)出的片源不带中文字幕,
# 用户希望这类组能被"降分"压到后段。与 前排/金标(加分) 对称 —— 这里做减分。
# 配置 `search.group_demote`(每项一个组名, 大小写不敏感, 整词匹配, 同 group_rank 口径):
#   - 键不存在 / 空      → 不降分(默认关, 老库向后兼容);
#   - 填了组名           → 命中的种子质量分 -20。
# ---------------------------------------------------------------------------
def group_demote_list(cfg):
    """降权组列表: 去空白、大小写不敏感去重(保留首次出现的写法); 默认空(不降分)。"""
    sec = (cfg or {}).get("search") or {}
    raw = sec.get("group_demote") or []
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


def demote_by(name, cfg):
    """种子名命中的"降权发布组"名(config `search.group_demote`, 整词+大小写不敏感); 无命中 None。"""
    n = (name or "").upper()
    if not n:
        return None
    for g in group_demote_list(cfg):
        if _group_re(g).search(n):
            return g
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
    demote = [g.lower() for g in group_demote_list(cfg)]
    return ("|".join(g.lower() for g in group_priority_list(cfg))
            + "#" + ",".join(golden) + "#" + ",".join(demote))


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


async def _load_window(source, cfg, q, need, sort, multi=False, ctype=None):
    """拿一个"至少 need 条"的全局排序窗口(分段抓取), 返回 (sorted_items, exhausted)。

    exhausted=True 表示站点已到底(再翻也不会有新结果)。
    分段的意义: 老实现每个请求都拉满 _SORT_CAP=200(站点每页 10 条, 串行翻 20 页 = 12~20s),
    首屏要等很久; 缓存 120s 过期后点一次「加载更多」又是十几秒 —— 2026-09-30 报的
    「详情页种子很慢 / 加载更多点了没反应」就出在这里。
    """
    need = max(1, int(need))
    key = _sort_key(q, sort, source, cfg)
    hit = _cache_get(key)
    base, cap0 = [], 0
    if hit:
        items, cap_used, exhausted = hit
        # 上限用"每源上限"而非全局 _SORT_CAP: 多源时 next_web 上限 100, 满了就该停
        if len(items) >= need or exhausted or cap_used >= _source_fetch_cap(source, multi):
            return items, exhausted
        base, cap0 = list(items), cap_used
    target = min(_SORT_CAP, max(need + _STEP, _WINDOW0), _source_fetch_cap(source, multi))
    site_done = False
    if cap0 and source == "next_web":
        # 增量续抓: 只补 target-cap0 条。旧写法命中缓存但条数不够就整段从第 1 页重抓 ——
        # 点一次「加载更多」把前面抓过的又抓一遍(6 次点击 ≈ 4 倍站点往返)。
        # TypeError = _fetch_all 被门禁打桩成不带 start_offset 的旧签名 → 退回整段抓。
        if target - cap0 <= 0:
            # 已到该源预算上限(如多源 next_web 100), 不再增量, 视为该源到底
            return _apply_sort(base, sort, cfg), True
        try:
            more = await _fetch_all(source, cfg, q, target - cap0, start_offset=cap0, multi=multi, ctype=ctype)
        except TypeError:
            more = None
        if more is not None:
            seen = {(x.get("infoHash") or "").lower() for x in base if x.get("infoHash")}
            got = base + [x for x in more
                          if not x.get("infoHash") or x["infoHash"].lower() not in seen]
            site_done = len(more) < (target - cap0)
        else:
            got = await _fetch_all(source, cfg, q, target, multi=multi, ctype=ctype)
            site_done = len(got) < target
    else:
        got = await _fetch_all(source, cfg, q, target, multi=multi, ctype=ctype)
        site_done = len(got) < target
    items = _apply_sort(got, sort, cfg)
    # 站点一条不剩(本页不满) → 到底; 满 target 条则认为后面还有, 下次要更多再抓
    exhausted = site_done
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
    """就地回传质量分/金标/前排组/降权: 双查询合并后前端按同规则重排, 徽章渲染。"""
    for it in items:
        it["qualityScore"] = quality_score(it.get("name"), cfg, it.get("size"))
        it["golden"] = is_golden(it.get("name"), cfg)
        it["goldenBy"] = golden_by(it.get("name"), cfg)  # 命中自压组名 → 前端标注"自压"
        db = demote_by(it.get("name"), cfg)              # 命中降权组 → 前端"降权"徽章
        it["demoted"] = bool(db)
        it["demotedBy"] = db
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


# 源标识 -> 配置段(每源各自一个 enabled 开关); 顺序即 _SOURCE_ORDER 的展示/合并顺序
_SOURCE_CFG_KEY = {"native": "bitmagnet", "next_web": "bitmagnet_next_web", "jackett": "jackett"}
_SOURCE_LABEL = {"native": "Bitmagnet", "next_web": "Bitmagnet-Next-Web", "jackett": "Jackett"}


def _enabled_sources(cfg):
    """返回启用的源(有序列表): 按 _SOURCE_ORDER 过滤掉未启用的。

    每个源各自一个 enabled 开关 —— 可只开一个, 也可同时开多个(多源时并行查询后合并去重)。
    段缺失(未配置)视为关, 所以老库没有 jackett 段时它天然不在列表里, 不会误触发 502。
    """
    return [s for s in _SOURCE_ORDER if _enabled(cfg, _SOURCE_CFG_KEY[s])]


def _src_label(source):
    """源标识 → 品牌名(错误信息、前端标签共用)。"""
    return _SOURCE_LABEL.get(source, source)


def _source_fetch_cap(source, multi):
    """Web 路径每源的抓取上限(按 性能×种子关联性 调过, 见 2026-10-03 分析)。

    - native:  100 —— 快(单查询)+高信号(seeders+元数据), 100 条足够, 再多是噪声。
    - jackett: 200 —— 一次抓全+有 seeders+自配站点(广+可信), 200 去重后够(原 500 在 Web 走不到)。
    - next_web:最慢(每页 10 条, 20 页)+最弱信号(无 seeders/元数据) →
      **多源时降到 100**(原生/Jackett 已覆盖 seeders+元数据+广度, 它的边际价值下降),
      **单源独开时保 200**(它是唯一来源, 覆盖要紧)。
    """
    if source == "next_web":
        return 100 if multi else 200
    return 100 if source == "native" else 200


async def _search_native(cfg, q, limit, ctype=None):
    from scripts import search as bm_search
    results = await run_in_threadpool(bm_search.search, cfg, q, limit, ctype)
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
            # 封面图: Bitmagnet 的 tmdb 元数据扩展写进 content.attributes 的海报(poster_path)
            "image": r.get("poster") or "",
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
            "image": "",       # 该站 REST 接口不带封面图(只有 hash/name/size/magnet/files)
        })
    # 精确续翻页号: 实际翻到的 offset 换算回 page(去重可能少走页, 不能按返回条数猜)
    next_page = end_offset // diao_search.PAGE_SIZE + 1
    return out, bool(more), next_page


def _local_img(url):
    """把"浏览器直连必裂图"的图源改写成本地图片代理路径(/api/img/<token>)。

    典型是 Jackett 的 coverurl: 它指向 Jackett 自身(常在内网), 浏览器跨网不可达 →
    统一交由服务端代取(见 server/imgproxy.py, 白名单按 jackett.base 动态放行)。
    TMDB 的海报由前端 img() 自己改写, 不走这里。空串原样返回; 编码异常退回原地址。
    """
    if not url:
        return ""
    try:
        from server import imgproxy
        return imgproxy.proxy_url(url)
    except Exception:  # noqa: BLE001
        return url


async def _search_jackett(cfg, q, limit):
    """Jackett(Torznab)源: 一次拿最多 limit 条(all 聚合上限 1000), 无 offset 续翻。

    has_more 按「返回条数 < limit」判断; next_page 恒为 1 —— Jackett 聚合不支持增量分页,
    前端据此不再对 jackett 续翻(与原生 GraphQL 一致: 不支持 page)。
    """
    from scripts import jackett_search
    items = await run_in_threadpool(jackett_search.search, cfg, q, limit)
    out = []
    for t in items:
        out.append({
            "infoHash": t.get("hash") or "",
            "name": t.get("name"),
            "size": t.get("size"),
            "seeders": t.get("seeders"),
            "leechers": t.get("leechers"),
            "magnet": t.get("magnet"),
            # 封面图: Jackett 的 torznab:attr coverurl(索引器提供的封面经它代理; 无封面的站为空)。
            # 该地址指向内网 Jackett, 改写成同源代理路径交给服务端代取。
            "image": _local_img(t.get("image")),
        })
    return out, len(out) < limit, 1


async def _fetch_all(source, cfg, q, cap, start_offset=0, multi=False, ctype=None):
    """抓最多 cap 条并规整成统一 item 结构(全局排序的取数原语, 也是 track_check 的入口)。

    顺序抓取由 scripts/diao_search.collect 内部并行化; 返回不足 cap 条 = 站点到底。
    start_offset: Next-Web 源的翻页偏移 —— 「加载更多」续抓时从已有条数接着抓,
    不再从第 1 页整段重来(原生搜索源不支持偏移, 该参数被忽略)。
    """
    if source == "jackett":
        from scripts import jackett_search
        # Jackett 聚合一次最多 limit 条(all 上限 1000), 无 offset 续抓
        items = await run_in_threadpool(jackett_search.search, cfg, q, min(cap, 500))
        return [{
            "infoHash": t.get("hash") or "", "name": t.get("name"), "size": t.get("size"),
            "seeders": t.get("seeders"), "leechers": t.get("leechers"), "magnet": t.get("magnet"),
            "image": _local_img(t.get("image")),
        } for t in items]
    if source == "next_web":
        from scripts import diao_search
        base, _ = diao_search.resolve_settings(cfg)
        # collect 内部按 offset 翻页, want=cap 一次拿全; 上限按 性能×关联性 随多源变化
        items, _tc, _kw, _more, _end = await run_in_threadpool(
            diao_search.collect, base, q, min(cap, _source_fetch_cap("next_web", multi)),
            start_offset=max(0, start_offset))
        out = []
        for t in items:
            out.append({
                "infoHash": t.get("hash"), "name": t.get("name"), "size": t.get("size"),
                "seeders": None, "leechers": None, "magnet": t.get("magnet"),
                "image": "",
            })
        return out
    else:
        from scripts import search as bm_search
        results = await run_in_threadpool(bm_search.search, cfg, q, min(cap, 100), ctype)
        out = []
        for r in results:
            magnet = r.get("magnetLink") or (
                f"magnet:?xt=urn:btih:{r['infoHash']}" if r.get("infoHash") else "")
            out.append({
                "infoHash": r.get("infoHash"), "name": r.get("name"), "size": r.get("size"),
                "seeders": r.get("seeders"), "leechers": r.get("leechers"), "magnet": magnet,
                "image": r.get("poster") or "",
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
            elif kind == "jackett":
                # Jackett(Torznab): GET {base}/api/v2.0/indexers/all/results/torznab 探测连通
                r = await c.get(url.rstrip("/") + "/api/v2.0/indexers/all/results/torznab",
                                headers={"Accept": "application/xml", "User-Agent": "media-auto"})
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
    地址没写路径时自动补 `/graphql`; `jackett` = Torznab 端点。
    """
    if kind not in ("rest", "graphql", "jackett"):
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


async def _one_page_relevance(source, cfg, q, limit, page, ctype=None):
    """单个源的 relevance 一页(引擎原序)。返回 (items, has_more, next_page)。"""
    if source == "next_web":
        return await _search_next_web(cfg, q, limit, page=page)
    if source == "jackett":
        return await _search_jackett(cfg, q, limit)
    return await _search_native(cfg, q, limit, ctype)


def _tag_source(items, source):
    """就地给每条打上 source 来源(前端来源标签/徽章用)。"""
    for it in items:
        it["source"] = source
    return items


def _dedup_key(it):
    """合并去重键: 优先 infoHash(小写); 无 hash 用 名称+大小。"""
    h = (it.get("infoHash") or "").lower()
    if h:
        return h
    return (it.get("name") or "") + "|" + str(it.get("size"))


def _merge_sources(per_items, sort, cfg):
    """把各源的已排序列表合并成一个(去重); 单源原样返回。多源按同一全局规则重排。"""
    if len(per_items) == 1:
        return per_items[0][1]
    seen, out = set(), []
    for _src, w in per_items:
        for it in w:
            k = _dedup_key(it)
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
    if sort != "relevance":
        _apply_sort(out, sort, cfg)
    return out


@router.get("")
async def api_search(q: str = Query(..., min_length=1), limit: int = 20,
                     page: int = Query(1, ge=1), sort: str = "relevance",
                     ctype: str = Query(None),
                     cfg: dict = Depends(get_config)):
    if not q.strip():
        raise HTTPException(400, "查询词不能为空")
    limit = max(1, min(int(limit or 20), 100))
    if sort not in _SORT_MODES:
        sort = "relevance"
    # ctype: Bitmagnet 内容类型过滤(movie/tv_show 等), 只认白名单, 其它值忽略
    if ctype and ctype not in _CTYPE_WHITELIST:
        ctype = None

    sources = _enabled_sources(cfg)
    if not sources:
        raise HTTPException(503, "所有磁力源都已禁用(管理 → 通用 → 磁力搜索源: 至少启用一个)")

    multi = len(sources) > 1

    # ---- 非 relevance: 各源【并行】各抓一个分段窗口 → 合并 → 全局排序 → 按 limit 切片 ----
    # 并行(asyncio.gather): 总耗时 = 最慢单源, 而非各源之和; 顺序仍按 sources 保证合并确定。
    if sort != "relevance":
        async def _one_nr(src):
            try:
                # 只抓"本页够用 + 一点余量"(首屏 60, 续翻按需 +30 递增, 上限按源)
                window, exhausted = await _load_window(src, cfg, q, page * limit, sort, multi=multi, ctype=ctype)
                _tag_source(window, src)
                return ("ok", (src, window, exhausted))
            except Exception as e:  # noqa: BLE001  单源失败不致命, 用其它源
                return ("warn", f"{_src_label(src)}: {e}")
        results = await asyncio.gather(*(_one_nr(src) for src in sources))
        per_items, warnings = [], []
        for kind, val in results:
            (warnings.append if kind == "warn" else per_items.append)(val)
        if not per_items:
            raise HTTPException(status_code=502, detail="; ".join(warnings) or "磁力源请求失败")
        window = _merge_sources([(s, w) for s, w, _e in per_items], sort, cfg)
        exhausted = all(ex for _s, _w, ex in per_items)

        start = (page - 1) * limit
        page_items = window[start:start + limit]
        # 还有下一页: 窗口里还有没展示的, 或窗口没抓满且站点未必到底(下次抓更大窗口)
        has_more = ((start + limit < len(window))
                    or (not exhausted and len(window) < _SORT_CAP * len(per_items)))
        # 回传前端: 双查询合并后按同规则重排, 前排/金标徽章渲染
        if sort == "quality":
            _annotate_quality(page_items, cfg)
        else:
            _annotate_group(page_items, cfg)
        _annotate_suspect(page_items)
        await run_in_threadpool(_annotate_pushed, page_items)   # 查库(队列合并)不占事件循环
        resp = {
            "source": sources[0], "sources": sources, "items": page_items,
            "hasMore": has_more, "nextPage": page + 1 if has_more else page, "sort": sort,
            "totalCount": len(window), "exhausted": exhausted,
        }
        if warnings:
            resp["warnings"] = warnings
        return resp

    # ---- relevance: 各源【并行】各取一页(引擎原序) → 合并去重 ----
    async def _one_rel(src):
        try:
            items, has_more, next_page = await _one_page_relevance(src, cfg, q, limit, page, ctype)
            _tag_source(items, src)
            return ("ok", (src, items, has_more, next_page))
        except Exception as e:  # noqa: BLE001
            return ("warn", f"{_src_label(src)}: {e}")
    results = await asyncio.gather(*(_one_rel(src) for src in sources))
    per_items, warnings = [], []
    for kind, val in results:
        (warnings.append if kind == "warn" else per_items.append)(val)
    if not per_items:
        raise HTTPException(status_code=502, detail="; ".join(warnings) or "磁力源请求失败")

    if len(per_items) == 1:
        # 单源: 原样取(与旧实现一致, 仅额外带 source 字段)
        items, has_more, next_page = per_items[0][1], per_items[0][2], per_items[0][3]
    else:
        # 多源: 按源序合并去重(relevance 保持各源引擎原序, 首源在前)
        seen, items = set(), []
        for _s, its, _hm, _np in per_items:
            for it in its:
                k = _dedup_key(it)
                if k in seen:
                    continue
                seen.add(k)
                items.append(it)
        has_more = any(hm for _s, _i, hm, _np in per_items)
        next_page = max((np for _s, _i, _hm, np in per_items), default=page + 1)

    _annotate_group(items, cfg)   # 前排徽章在相关性排序下也照常显示
    _annotate_suspect(items)
    await run_in_threadpool(_annotate_pushed, items)            # 查库(队列合并)不占事件循环
    resp = {"source": sources[0], "sources": sources, "items": items,
            "hasMore": has_more, "nextPage": next_page, "sort": sort}
    if warnings:
        resp["warnings"] = warnings
    return resp
