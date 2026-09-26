#!/usr/bin/env python3
r"""
media-auto 分类引擎
==================

根据媒体元数据(标题/类型/语言/国家)决定目标媒体库目录。分级(certification/adult)
已不参与归类,只写进 NFO 交给影片服务器控制(见下方设计要点)。

优先级级联(类型优先于地区):
    动画(Dm) > 纪录片(Jl) > 综艺(Xr) > 体育(Sp) > 音乐(Mu) > 地区(Cn/En/JpKr/Hk/Sea/Ot)

设计要点:
- 所有目录名都在配置的 categories 里(管理 → 通用 → 分类),可随意改名/增删。
- 综艺(Xr)/体育(Sp)/音乐(Mu)只有 Show 变体(本质是系列),电影落到地区。
- 纪录片不分 Movie/Show,统一进 JlShow(含单部纪录电影)。
- ⚠️ 不再按 18+ 分级单独分目录(2026-09-22 用户决定移除 Ts 级联):
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
      3) 分类键: server/routers/cd2.py::_CATEGORY_META 补 TsMovie/TsShow,
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

# 语言代码 -> 地区键 (En = 欧美,涵盖主要欧洲语言)
LANG_REGION = {
    'zh': 'Cn', 'cn': 'Cn', 'yue': 'Hk',
    'en': 'En', 'fr': 'En', 'de': 'En', 'es': 'En', 'it': 'En', 'pt': 'En',
    'ru': 'En', 'nl': 'En', 'pl': 'En', 'sv': 'En', 'da': 'En', 'no': 'En',
    'fi': 'En', 'tr': 'En', 'el': 'En', 'cs': 'En', 'hu': 'En', 'ro': 'En',
    'ja': 'JpKr', 'ko': 'JpKr',
    'th': 'Sea', 'vi': 'Sea', 'id': 'Sea', 'ms': 'Sea', 'tl': 'Sea', 'my': 'Sea',
}
# 国家/地区代码 -> 地区键
COUNTRY_REGION = {
    'CN': 'Cn', 'HK': 'Hk', 'TW': 'Hk',
    'US': 'En', 'GB': 'En', 'FR': 'En', 'DE': 'En', 'CA': 'En', 'AU': 'En',
    'JP': 'JpKr', 'KR': 'JpKr',
    'TH': 'Sea', 'VN': 'Sea', 'ID': 'Sea', 'MY': 'Sea', 'SG': 'Sea', 'PH': 'Sea',
}
HK_KW = [r'港', r'台', r'粤语', r'港剧', r'台剧', r'港澳台']

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


# 分级白名单(按段精确匹配, 命中任一即视为 18+):
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


def _region(media, text):
    lang = (media.get('language') or '').lower()
    countries = [str(c).upper() for c in (media.get('countries') or [])]
    # 语言是否中文语系(粤语/普通话/国语)。港片·台片几乎都是这两种语言,
    # 用它来收紧"TW 产地→港台"的判定, 避免国际合拍片被误判。
    is_cn_lang = lang in ('cn', 'zh', 'yue')

    # 港台(Hk)判定(优先级):
    #  - 粤语(yue): 直接判港;
    #  - 产地含 HK: 直接判港(香港产地是港片的最强信号, 不受语言约束);
    #  - 标题·文件名含 港/台/粤语 关键词: 强信号, 不受语言约束;
    #  - 产地含 TW: **仅当语言是中文语系(cn/zh/yue)** 才判港台 ——
    #    国际合拍片(如《麦哲伦》2025: 葡萄牙语 + 菲律宾/葡萄牙/西班牙/法国/台湾 五地合拍,
    #    tmdb 把台湾也列进 production_countries)里台湾只是次要合拍地, 若一律判港台会误进 HkMovie;
    #    而真正的港片/台片语言就是粤语或普通话, is_cn_lang 为 True, 不受影响。
    if (lang == 'yue'
            or 'HK' in countries
            or _match(HK_KW, text)
            or ('TW' in countries and is_cn_lang)):
        return 'Hk'

    if lang in LANG_REGION:
        return LANG_REGION[lang]
    for c in countries:
        if c in COUNTRY_REGION:
            return COUNTRY_REGION[c]
    return 'Ot'


def classify(media, config=None):
    """
    参数:
        media: dict, 至少包含:
            title (str), content_type ('movie'|'tv'),
            genres (list[int|str]), language (str), countries (list[str]),
            filename (str, 可选)
            注: adult/certification 已不再参与归类(见模块 docstring), 传了也不影响结果。
        config: 解析后的配置(dict),用于取 categories 目录名
    返回:
        dict { category_key, folder, content_type, reasons[] }
    """
    config = config or {}
    categories = config.get('categories', {})

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
    region = _region(media, text)
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
