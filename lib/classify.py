#!/usr/bin/env python3
r"""
media-auto 分类引擎
==================

根据媒体元数据(标题/类型/语言/国家)决定目标媒体库目录。分级(certification/adult)
已不参与归类,只写进 NFO 交给影片服务器控制(见下方设计要点)。

优先级级联(类型优先于地区):
    动画(Dm) > 纪录片(Jl) > 综艺(Xr) > 体育(Sp) > 音乐(Mu) > 地区档
    地区档默认 Cn/En/JpKr/Hk/Sea/Ot, 可在「管理 → 分类规则 → 地区档」增删改
    (每档生成 <键>Movie/<键>Show 两个分类键), 配置键 config.regions。

设计要点:
- 分类键两类: 特殊类型键固定(6 个), 地区键随地区档配置变; 目录名都在
  config.categories 里(管理 → 分类规则), 可随意改名。
- 综艺(Xr)/体育(Sp)/音乐(Mu)只有 Show 变体(本质是系列), 电影落到地区。
- 纪录片不分 Movie/Show,统一进 JlShow(含单部纪录电影)。
- ⚠️ 不再按 18+ 级别单独分目录(2026-09-22 用户决定移除 Ts 级联):
    分级照旧原样写进 NFO 的 <mpaa>/<certification>, 由影片服务器(Jellyfin)按分级
    做访问控制/显示控制 —— 用目录隔离成人内容既与服务器功能重复, 又死板(同一部片
    永远只能落一个目录)。故原"18+ 最高优先级 + Ts*"整段已删除。

    【如何恢复】(本仓库 git 无提交, 不要指望 git 历史; 按下面三步即可复原)
      1) const: 补回关键词兜底表(仅 18+ 用)
           ADULT_KW = [r'18\+', r'(?<![A-Za-z0-9])r18(?![A-Za-z0-9])',
                       r'(?<![A-Za-z0-9])xxx(?![A-Za-z0-9])', r'(?<![A-Za-z0-9])av(?![A-Za-z0-9])',
                       r'无码', r'有码', r'里番', r'福利', r'tsuna', r'uncensored',
                       r'(?<![A-Za-z0-9])adult(?![A-Za-z0-9])', r'成人的']
         并在 _is_adult() 末尾加回两行: `if _match(ADULT_KW, text): return True`
      2) classify(): 在 genres = _norm_genres(...) 之前插回
           if _is_adult(media, text):
               reasons.append('adult/18+ signal')
               return _emit('Ts', ct, categories, reasons)
      3) 分类键: server/routers/cd2.py::_SPECIAL_META 补 TsMovie/TsShow,
         配置的 categories 补 "TsMovie"/"TsShow"
         (并把本文件的级联 docstring 改回 18+ 在最前)。
"""
import json
import os
import re
import sys
import argparse

# ---------------------------------------------------------------------------
# 关键词兜底表(当 TMDB 类型/语言缺失时,从标题/文件名猜)
# ---------------------------------------------------------------------------
# 注: 用 (?<![A-Za-z0-9])/(?![A-Za-z0-9]) 代替 \b, 因为 \b 在 CJK 旁(中日韩属"单词字符")会失效。
ANIME_KW = [r'动漫', r'动画', r'番剧', r'(?<![A-Za-z0-9])anime(?![A-Za-z0-9])', r'番组', r'新番']
DOC_KW = [r'纪录片', r'纪录', r'(?<![A-Za-z0-9])documentary(?![A-Za-z0-9])', r'记录片']
VARIETY_KW = [r'综艺', r'脱口秀', r'真人秀', r'(?<![A-Za-z0-9])talk\s?show(?![A-Za-z0-9])', r'访谈', r'搞笑节目', r'喜剧节目']
SPORTS_KW = [r'体育', r'赛事', r'世界杯', r'(?<![A-Za-z0-9])nba(?![A-Za-z0-9])', r'足球', r'(?<![A-Za-z0-9])sport', r'电竞', r'篮球', r'英超', r'欧冠']
MUSIC_KW = [r'演唱会', r'音乐', r'(?<![A-Za-z0-9])mv(?![A-Za-z0-9])', r'现场', r'concert', r'专辑', r'现场版', r'(?<![A-Za-z0-9])live(?![A-Za-z0-9])']

# TMDB genre id
GENRE_ANIMATION = 16
GENRE_DOCUMENTARY = 99

HK_KW = [r'港', r'台', r'粤语', r'港剧', r'台剧', r'港澳台']

# ---------------------------------------------------------------------------
# 地区档(可配置)—— 归属 / 顺序 / 新增档都在「管理 → 分类规则 → 地区分类」里改,
# 这里只是出厂默认(配置里没有 regions 时用它, 保证老库行为一字不变)。
#
# 每档字段:
#   label     档名(分类规则页表头, 如「港台」)
#   display   详情页「地区」显示名(如「港片」), 空则回退 label
#   languages 语言归属 → 这些语言判进本档(在国家之前)
#   countries 国家归属 → 这些产地判进本档(在语言之后, 适合"说哪种话就是哪的片")
#   keywords  标题/文件名关键词命中即判本档(最先判)
#   prio      优先国家 {国家码: [允许的语言]} —— **先于语言**判定;
#             语言留空 = 不限(香港 HK), 写了 = 只有这些语言才算(TW=中文系)
#
# 判定顺序(见 _region): 关键词 → 优先国家 → 语言归属 → 归属国家 → Ot
# ---------------------------------------------------------------------------
DEFAULT_REGIONS = {
    'order': ['Cn', 'En', 'JpKr', 'Hk', 'Sea', 'Ot'],
    'items': {
        'Cn': {'label': '中国大陆', 'display': '国片', 'languages': ['zh', 'cn'],
               'countries': ['CN'], 'keywords': [], 'prio': {}},
        'En': {'label': '欧美', 'display': '欧美',
               'languages': ['en', 'fr', 'de', 'es', 'it', 'pt', 'ru', 'nl', 'pl',
                             'sv', 'da', 'no', 'fi', 'tr', 'el', 'cs', 'hu', 'ro'],
               'countries': ['US', 'GB', 'FR', 'DE', 'CA', 'AU'], 'keywords': [], 'prio': {}},
        'JpKr': {'label': '日韩', 'display': '日韩', 'languages': ['ja', 'ko'],
                 'countries': ['JP', 'KR'], 'keywords': [], 'prio': {}},
        'Hk': {'label': '港台', 'display': '港片', 'languages': ['yue'],
               'countries': ['HK', 'TW'], 'keywords': list(HK_KW),
               'prio': {'HK': [], 'TW': ['cn', 'zh', 'yue']}},
        'Sea': {'label': '东南亚', 'display': '东南亚',
                'languages': ['th', 'vi', 'id', 'ms', 'tl', 'my'],
                'countries': ['TH', 'VN', 'ID', 'MY', 'SG', 'PH'], 'keywords': [], 'prio': {}},
        'Ot': {'label': '其他', 'display': '其他', 'languages': [], 'countries': [],
               'keywords': [], 'prio': {}},
    },
}
# 特殊类型档(分类键 Dm*/Jl*/Xr*/Sp*/Mu*), 地区档键不能与之同名
SPECIAL_PREFIXES = ('Dm', 'Jl', 'Xr', 'Sp', 'Mu')
# 分类键后缀: 每个地区档生成 <键>Movie / <键>Show 两个分类键
CAT_SUFFIXES = ('Movie', 'Show')
# 兜底档(判定不到任何地区时落这里), 不允许删除
FALLBACK_REGION = 'Ot'

_REGION_KEY_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,14}$')
_COUNTRY_CODE_RE = re.compile(r'^[A-Za-z]{2}$')
_LANG_CODE_RE = re.compile(r'^[A-Za-z]{2,3}$')

# 仅 Show 的类型(没有 Movie 变体)
SHOW_ONLY_TYPES = {'Xr', 'Sp', 'Mu'}


def _match(patterns, text):
    t = (text or '').lower()
    for p in patterns:
        if re.search(p, t, re.IGNORECASE):
            return True
    return False


def _norm_genres(genres):
    out = []
    for g in genres or []:
        if isinstance(g, int):
            out.append(g)
        elif isinstance(g, str):
            g = g.strip()
            if g.isdigit():
                out.append(int(g))
            else:
                # 把常见英文名映射成 id
                name = g.lower()
                if 'animation' in name or '动画' in g:
                    out.append(GENRE_ANIMATION)
                elif 'documentary' in name or '纪录' in g:
                    out.append(GENRE_DOCUMENTARY)
                else:
                    out.append(g)
    return out


# ---------------------------------------------------------------------------
# 地区配置的读取(宽松)与校验(严格)
# ---------------------------------------------------------------------------
def _as_list(v):
    """任意"列表"输入 → 去空、去重的 list(单个字符串也接受)。"""
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    out = []
    for x in (v if isinstance(v, (list, tuple, set)) else []):
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _as_codes(v):
    """码表(国家/语言)输入 → 去空去重的码列表。

    字符串按空白与逗号拆 —— 手改配置或外部脚本常写成 "zh cn" / "HK,TW";
    关键词走 _as_list 不拆(关键词本身可以带空格)。
    """
    if isinstance(v, str):
        v = [x for x in re.split(r"[\s,]+", v) if x]
    return _as_list(v)


def normalize_regions(regions=None):
    """配置里的 regions → 可直接用的结构(缺项补默认、脏值清洗, **不抛错**)。

    返回 {order, items, lang_map, country_map, prio_map};
    regions 为空/没配过 = 出厂默认(老库行为不变)。
    """
    src = regions if isinstance(regions, dict) else {}
    raw_items = src.get('items') if isinstance(src.get('items'), dict) else {}
    if not _as_list(src.get('order')) and not raw_items:
        src = DEFAULT_REGIONS
        raw_items = src['items']

    order = _as_list(src.get('order'))
    for k in raw_items:                       # 只在 items 里的键 → 追到末尾
        k = str(k).strip()
        if k and k not in order:
            order.append(k)
    if FALLBACK_REGION not in order:          # 兜底档永远在
        order.append(FALLBACK_REGION)

    items = {}
    for key in order:
        base = DEFAULT_REGIONS['items'].get(key) or {'label': key, 'display': key}
        raw = raw_items.get(key)
        raw = raw if isinstance(raw, dict) else {}
        prio_src = raw.get('prio', base.get('prio'))
        prio = {}
        if isinstance(prio_src, dict):
            for c, langs in prio_src.items():
                c = str(c).strip().upper()
                if not _COUNTRY_CODE_RE.match(c):
                    continue
                prio[c] = [s.lower() for s in _as_codes(langs)
                           if _LANG_CODE_RE.match(str(s).strip().lower() or 'x')]
        items[key] = {
            'label': (str(raw.get('label') or base.get('label') or key).strip()[:30] or key),
            'display': str(raw.get('display', base.get('display', '')) or '').strip()[:30],
            'languages': [s.lower() for s in _as_codes(
                raw.get('languages', base.get('languages'))) if _LANG_CODE_RE.match(s.lower() or 'x')],
            'countries': [s.upper() for s in _as_codes(
                raw.get('countries', base.get('countries'))) if _COUNTRY_CODE_RE.match(s.upper() or 'xx')],
            'keywords': _as_list(raw.get('keywords', base.get('keywords')))[:20],
            'prio': prio,
        }

    lang_map, country_map, prio_map = {}, {}, {}
    for key in order:
        it = items[key]
        for lg in it['languages']:
            lang_map.setdefault(lg, key)
        for c in it['countries']:
            country_map.setdefault(c, key)
    for key in order:                          # 优先国家兜底也进国家表(语言判不出时用)
        for c, langs in items[key]['prio'].items():
            prio_map[c] = (key, langs)
            country_map.setdefault(c, key)
    return {'order': order, 'items': items, 'lang_map': lang_map,
            'country_map': country_map, 'prio_map': prio_map}


def validate_regions(regions):
    """严格校验「管理 → 分类规则」提交的地区配置, 不合法抛 ValueError(中文, 直接回前端)。

    校验点: 结构 / 键名 / 与特殊类型不撞车 / Ot 必留 / 码表格式 /
            国家与语言不能同时属于两个档。
    """
    if regions is None:
        return None
    if not isinstance(regions, dict):
        raise ValueError('地区配置必须是对象 {order, items}')
    order, items = regions.get('order'), regions.get('items')
    if not isinstance(order, list) or not isinstance(items, dict):
        raise ValueError('地区配置缺少 order/items')
    keys = [str(k).strip() for k in order]
    if not keys:
        raise ValueError('至少要有一个地区档')
    if len(set(keys)) != len(keys):
        raise ValueError('地区档顺序里有重复的键')
    if set(keys) != {str(k).strip() for k in items}:
        raise ValueError('地区档的「顺序」与「内容」不一致(有键没保存内容,或反之)')
    for k in keys:
        if not _REGION_KEY_RE.match(k):
            raise ValueError(f'地区档键不合法: {k}(字母开头, 只能含字母/数字/下划线, ≤15 字符)')
        if k + CAT_SUFFIXES[0] in {p + s for p in SPECIAL_PREFIXES for s in CAT_SUFFIXES}:
            raise ValueError(f'地区档键 {k} 与特殊类型(动画/纪录片/综艺/体育/音乐)的分类键冲突')
    if FALLBACK_REGION not in keys:
        raise ValueError(f'必须保留兜底档 {FALLBACK_REGION}(其他), 判不出地区时靠它承接')
    if len(keys) > 40:
        raise ValueError('地区档最多 40 个')

    seen_country, seen_lang = {}, {}
    for k in keys:
        it = items[k]
        if not isinstance(it, dict):
            raise ValueError(f'地区档 {k} 的内容必须是对象')
        label = str(it.get('label') or '').strip()
        if not label:
            raise ValueError(f'地区档 {k} 的档名不能为空')
        if len(label) > 30 or re.search(r'[\x00-\x1f]', label):
            raise ValueError(f'地区档 {k} 的档名不合法(≤30 字符, 不能含控制字符)')
        countries = _as_codes(it.get('countries'))
        prio = it.get('prio') or {}
        if not isinstance(prio, dict):
            raise ValueError(f'地区档 {k} 的优先国家必须是对象')
        languages = _as_codes(it.get('languages'))
        keywords = _as_list(it.get('keywords'))
        if len(countries) > 40 or len(languages) > 40 or len(keywords) > 20:
            raise ValueError(f'地区档 {k} 的条目过多(国家≤40 语言≤40 关键词≤20)')
        for kw in keywords:
            if len(kw) > 40 or re.search(r'[\x00-\x1f]', kw):
                raise ValueError(f'地区档 {k} 的关键词不合法: {kw[:20]}(≤40 字符)')
        for code, where in [(c, '归属国家') for c in countries] + [(c, '优先国家') for c in prio]:
            if not _COUNTRY_CODE_RE.match(str(code).strip()):
                raise ValueError(f'地区档 {k} 的{where}代码不合法: {code}(两位字母, 如 TW)')
            c = str(code).strip().upper()
            # 同一档里"归属国家"与"优先国家"允许重复(优先=先于语言, 归属=兜底)
            if c in seen_country and seen_country[c][0] != k:
                raise ValueError(f'国家 {c} 同时出现在「{seen_country[c][1]}」和「{label}」, 只能属于一个档'
                                 f'(要把它单拆一档, 请先从原档的「归属国家」里删掉)')
            seen_country[c] = (k, label)
        for c, langs in prio.items():
            for lg in _as_codes(langs):
                if not _LANG_CODE_RE.match(str(lg).strip()):
                    raise ValueError(f'地区档 {k} 的优先条件语言不合法: {lg}(2~3 位字母, 如 zh)')
        for lg in languages:
            if not _LANG_CODE_RE.match(str(lg).strip()):
                raise ValueError(f'地区档 {k} 的语言代码不合法: {lg}(2~3 位字母, 如 ja)')
            lg = str(lg).strip().lower()
            if lg in seen_lang and seen_lang[lg][0] != k:
                raise ValueError(f'语言 {lg} 同时属于「{seen_lang[lg][1]}」和「{label}」, 只能属于一个档'
                                 f'(语言是全局归属; 要按"某国 + 某语言"细分, 用「优先国家」)')
            seen_lang[lg] = (k, label)
    return None


def region_category_keys(regions):
    """地区档 → 分类键清单(每档 Movie/Show 两个), 如 Tw → [TwMovie, TwShow]。"""
    norm = regions if isinstance(regions, dict) and 'items' in regions else normalize_regions(regions)
    return [k + s for k in norm['order'] for s in CAT_SUFFIXES]


def region_info(media, config=None):
    """详情页「地区」显示: {key, label(display 名)}—— 与 organize 分类同一套口径。"""
    config = config or {}
    norm = normalize_regions(config.get('regions'))
    text = ' '.join([str(media.get('title', '')), str(media.get('filename', ''))])
    key = _region(media, text, norm)
    item = norm['items'].get(key) or {}
    return {'key': key, 'label': item.get('display') or item.get('label') or key}

#   III      = 香港三级(竹夫人/三夫这类情色片, 也含黑社会/力王这类暴力片 —— 分级本身就是"18岁以上才可观看")
#   18       = GB:18 / KR:18 等成人级
#   R18/R18+ = GB / 澳;  NC-17 / TV-MA / XXX / X = 美区成人级
#
# ⚠️ 2026-09-22: 用户决定不再用"目录分类"隔离成人内容(改由 Jellyfin 按分级控制),
# 故 classify() 的 Ts 分支已删除(ADULT_KW 常量也一并删了)。
# ADULT_CERTS / _cert_segments / _is_adult 保留下来, 但**不再被 classify() 调用** ——
# 它们现在只服务于"某一条目是不是成人分级"这种单点查询(调试/外部脚本)。
# 分级数据本身仍完整保留: detail() 照旧输出 certification → 写进 NFO 的
# <mpaa>/<certification>, Jellyfin 靠它做访问控制。
# 【恢复 18+ 分类的具体步骤见本文件顶部模块 docstring】
ADULT_CERTS = {'III', '18', 'R18', 'R18+', 'NC-17', 'TV-MA', 'XXX', 'X'}


def _cert_segments(cert):
    """'HK:III / HK:IIB' / 'US:NC-17' / 'R18' → [('HK','III'), ('HK','IIB'), ...]

    按段精确匹配, 所以 IIA/IIB 不会误中 III; 兼容尾随空格('HK:III ')。
    """
    out = []
    for seg in re.split(r'[/,;]', (cert or '').upper()):
        seg = seg.strip()
        if not seg:
            continue
        if ':' in seg:
            cc, val = seg.split(':', 1)
            out.append((cc.strip(), val.strip()))
        else:
            out.append(('', seg))
    return out


def _is_adult(media, text):
    """⚠️ 已不再被 classify() 调用(2026-09-22 移除 Ts 级联)。

    保留此函数仅为外部脚本/调试手工判断"是否成人分级"时复用:
    它只回答"是不是成人分级", 不再影响归类目录。
    """
    if media.get('adult') is True:
        return True
    # 分级按段解析命中(2026-09-22 修): 旧写法 `cert in (...) and '18' in cert`
    # 实际只有 R18 能通过(XXX/NC-17/TV-MA 都不含 '18', 恒 False), 且不认香港 III。
    for _cc, val in _cert_segments(media.get('certification')):
        if val in ADULT_CERTS:
            return True
    return False


def _region(media, text, regions=None):
    """地区档判定(可配置): 关键词 → 优先国家(先于语言, 可带语言条件) → 语言 → 国家 → Ot。

    regions 缺省时用 normalize_regions(None) = 出厂默认, 行为与旧版硬编码一致:
    - 港片强信号(HK 产地 / 港台粤语关键词)先于语言 —— 港片 original_language 常是 cn;
    - TW 只在中文系语言下判港台 —— 国际合拍片里台湾只是次要合拍地, 不该进港台目录;
    - 其余按 语言 → 国家 的顺序, 都判不出落 Ot。
    """
    regions = regions if (isinstance(regions, dict) and 'lang_map' in regions) \
        else normalize_regions(regions)
    lang = (media.get('language') or '').lower().strip()
    countries = [str(c).upper().strip() for c in (media.get('countries') or []) if str(c).strip()]
    items = regions['items']

    # 1) 标题/文件名关键词(按档顺序)
    for key in regions['order']:
        kws = items[key]['keywords']
        if kws and _match(kws, text):
            return key

    # 2) 优先国家(先于语言判定; 值 = 允许的语言, 空 = 不限)
    for c in countries:
        hit = regions['prio_map'].get(c)
        if hit:
            key, guard = hit
            if not guard or lang in guard:
                return key

    # 3) 语言归属
    if lang in regions['lang_map']:
        return regions['lang_map'][lang]

    # 4) 归属国家(按条目里国家的先后)
    for c in countries:
        if c in regions['country_map']:
            return regions['country_map'][c]

    return FALLBACK_REGION


def classify(media, config=None):
    """
    参数:
        media: dict, 至少包含:
            title (str), content_type ('movie'|'tv'),
            genres (list[int|str]), language (str), countries (list[str]),
            filename (str, 可选)
            注: adult/certification 已不再参与归类(见模块 docstring), 传了也不影响结果。
        config: 解析后的配置(dict)。categories 取目录名; regions 取地区档配置
                (归属/顺序/新增档, 管理 → 分类规则改; 没配 = 出厂默认)
    返回:
        dict { category_key, folder, content_type, reasons[] }
    """
    config = config or {}
    categories = config.get('categories', {})
    regions = normalize_regions(config.get('regions'))

    ct = 'show' if str(media.get('content_type', '')).lower() in ('tv', 'show') else 'movie'
    text = ' '.join([str(media.get('title', '')), str(media.get('filename', ''))])
    reasons = []

    genres = _norm_genres(media.get('genres', []))
    is_anime = (GENRE_ANIMATION in genres) or _match(ANIME_KW, text)
    is_doc = (GENRE_DOCUMENTARY in genres) or _match(DOC_KW, text)

    # 1) 类型层(综艺/体育/音乐只在 show 分支)
    if ct == 'show':
        if is_doc:
            reasons.append('genre=documentary')
            return _emit('Jl', 'show', categories, reasons)
        if is_anime:
            reasons.append('genre=animation')
            return _emit('Dm', 'show', categories, reasons)
        if _match(VARIETY_KW, text):
            reasons.append('keyword=variety')
            return _emit('Xr', 'show', categories, reasons)
        if _match(SPORTS_KW, text):
            reasons.append('keyword=sports')
            return _emit('Sp', 'show', categories, reasons)
        if _match(MUSIC_KW, text):
            reasons.append('keyword=music')
            return _emit('Mu', 'show', categories, reasons)
    else:  # movie
        if is_anime:
            reasons.append('genre=animation')
            return _emit('Dm', 'movie', categories, reasons)
        if is_doc:
            # 纪录电影也进 JlShow
            reasons.append('genre=documentary -> JlShow')
            return _emit('Jl', 'show', categories, reasons)

    # 2) 地区层
    region = _region(media, text, regions)
    reasons.append('region=' + region)
    return _emit(region, ct, categories, reasons)



def _emit(key, ct, categories, reasons):
    suffix = 'Movie' if ct == 'movie' else 'Show'
    cat_key = key + suffix
    folder = categories.get(cat_key, cat_key)  # 没配就回退用 key 名
    return {
        'category_key': cat_key,
        'region_type': key,
        'content_type': ct,
        'folder': folder,
        'reasons': reasons,
    }


def load_config():
    # 统一走 lib/config.py 的 load_config(全库唯一实现; 原本地副本已删)
    from lib.config import load_config as _lc
    return _lc()


def main():
    ap = argparse.ArgumentParser(description='media-auto 分类引擎测试')
    ap.add_argument('--title', required=True)
    ap.add_argument('--content-type', choices=['movie', 'tv'], default='movie')
    ap.add_argument('--genres', default='', help='逗号分隔的 genre id 或名称')
    ap.add_argument('--language', default='')
    ap.add_argument('--countries', default='', help='逗号分隔 ISO 国家码')
    ap.add_argument('--adult', action='store_true', help='已废弃: 不再影响分类(仅调试用)')
    ap.add_argument('--certification', default='', help='已废弃: 不再影响分类(仅调试用)')
    ap.add_argument('--filename', default='')
    args = ap.parse_args()

    media = {
        'title': args.title,
        'content_type': args.content_type,
        'genres': [g.strip() for g in args.genres.split(',') if g.strip()],
        'language': args.language,
        'countries': [c.strip() for c in args.countries.split(',') if c.strip()],
        'adult': args.adult,
        'certification': args.certification,
        'filename': args.filename,
    }
    cfg = load_config()
    result = classify(media, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
