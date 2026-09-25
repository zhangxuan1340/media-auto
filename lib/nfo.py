#!/usr/bin/env python3
"""tinyMediaManager(5.2.12, JELLYFIN profile) 兼容 NFO 生成
================================================================
目标: 自己产出完整 NFO, 让 tinyMediaManager / Jellyfin **不需要再刮削**。

数据来源:
  - 元数据(标题/年份/剧情/演职员/关键词/分级...): TMDB 详情接口
    (clients/tmdb/client.py -> detail_sync, 已规整出 NFO 所需全部字段)
  - 媒体技术信息(编码/分辨率/音轨/字幕): MediaInfo CLI
    (lib/mediainfo.py -> probe_file / streamdetails_xml)
  - wikidata id: Wikidata SPARQL(按 IMDb id 反查;TMDB 不给这个)

⚠️ 两个"拿不到"的字段(已知缺口, 不编造):
  1. **IMDb 评分**: imdb.com 页面/GraphQL 在本环境返回空,所以 <ratings> 只写 themoviedb
     (并把 default="true" 给它)。TMM 后续若联网会自己补上 IMDb 那一条。
  2. **belongsToCollection**: 通常拿不到合集,所以 <set> 一般为自闭合空标签。
  其他如 tvdb id(剧集)、english_title(非中文原名时) 都能正常给出。

格式基准(直接从用户库里 TMM 5.2.12 产出的 NFO 反推, 逐字段对齐):
  - 电影: <movie>, codec 小写(hevc/eac3), 含 <resolution>, actor 带 <thumb>,
          导演独立 <director tmdbid="...">, 末尾统一 <crew><role subrole="...">
  - 剧集: <tvshow>, 多 <showtitle>/<namedseason>/<episodeguide>/<status>/<enddate>,
          uniqueid 以 tvdb 为 default
  - 结尾都有 <tmm_locked/>(锁定后 TMM 不会再改写, 这正是"不需要它刮削"的实现)
"""
import html
import json
import re
import time
import urllib.parse
import urllib.request

TMM_VERSION = "5.2.12"
TMM_HEADER = "tinyMediaManager {v} for JELLYFIN".format(v=TMM_VERSION)

# ---------------------------------------------------------------------------
# 国家/地区 ISO 3166-1 alpha-2 -> 中文名
# ---------------------------------------------------------------------------
COUNTRY_ZH = {
    "CN": "中国", "HK": "中国香港", "MO": "中国澳门", "TW": "中国台湾",
    "US": "美国", "GB": "英国", "UK": "英国", "CA": "加拿大", "AU": "澳大利亚",
    "NZ": "新西兰", "IE": "爱尔兰", "FR": "法国", "DE": "德国", "IT": "意大利",
    "ES": "西班牙", "PT": "葡萄牙", "NL": "荷兰", "BE": "比利时", "CH": "瑞士",
    "AT": "奥地利", "SE": "瑞典", "NO": "挪威", "DK": "丹麦", "FI": "芬兰",
    "IS": "冰岛", "PL": "波兰", "CZ": "捷克", "HU": "匈牙利", "RO": "罗马尼亚",
    "RU": "俄罗斯", "UA": "乌克兰", "GR": "希腊", "TR": "土耳其",
    "JP": "日本", "KR": "韩国", "KP": "朝鲜", "IN": "印度", "TH": "泰国",
    "VN": "越南", "MY": "马来西亚", "SG": "新加坡", "ID": "印度尼西亚",
    "PH": "菲律宾", "MM": "缅甸", "KH": "柬埔寨", "LA": "老挝", "MN": "蒙古",
    "IL": "以色列", "IR": "伊朗", "IQ": "伊拉克", "SA": "沙特阿拉伯",
    "AE": "阿联酋", "EG": "埃及", "ZA": "南非", "NG": "尼日利亚",
    "MX": "墨西哥", "BR": "巴西", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "PE": "秘鲁", "VE": "委内瑞拉", "UY": "乌拉圭", "CU": "古巴",
    "SU": "苏联", "YU": "南斯拉夫", "CS": "捷克斯洛伐克", "DD": "东德",
}

# ---------------------------------------------------------------------------
# 语言 ISO 639-1 -> 中文名(对应 TMM 的 <languages>)
# ---------------------------------------------------------------------------
LANG_ZH = {
    "zh": "中文", "cn": "中文", "en": "英语", "ja": "日语", "jp": "日语",
    "ko": "韩语", "kr": "韩语", "fr": "法语", "de": "德语", "es": "西班牙语",
    "it": "意大利语", "pt": "葡萄牙语", "ru": "俄语", "ar": "阿拉伯语",
    "hi": "印地语", "th": "泰语", "vi": "越南语", "id": "印度尼西亚语",
    "ms": "马来语", "tl": "他加禄语", "nl": "荷兰语", "sv": "瑞典语",
    "no": "挪威语", "da": "丹麦语", "fi": "芬兰语", "pl": "波兰语",
    "cs": "捷克语", "hu": "匈牙利语", "ro": "罗马尼亚语", "el": "希腊语",
    "tr": "土耳其语", "he": "希伯来语", "fa": "波斯语", "uk": "乌克兰语",
    "ca": "加泰罗尼亚语", "sr": "塞尔维亚语", "hr": "克罗地亚语", "bg": "保加利亚语",
    "yue": "粤语", "la": "拉丁语", "is": "冰岛语",
}

# 源类型(TMM <source> 取值)
SOURCE_MAP = {
    "BLURAY": "BLURAY", "BLU-RAY": "BLURAY", "BDRIP": "BLURAY", "REMUX": "BLURAY",
    "UHD": "BLURAY", "WEB-DL": "WEBDL", "WEBDL": "WEBDL", "WEBRIP": "WEBRIP",
    "HDTV": "HDTV", "DVDRIP": "DVD", "DVD": "DVD", "HDDVD": "HDDVD",
    "VHS": "VHS", "VOD": "VOD", "TV": "TV", "HDTS": "HDTV", "TS": "HDTV",
    "CAM": "HDTV", "HDRIP": "WEBRIP",
}

SPARQL_URL = "https://query.wikidata.org/sparql"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def xml_esc(value):
    """XML **属性值**转义(引号也要转,否则属性会被截断)。"""
    return html.escape(str(value), quote=True)


def xml_text(value):
    """XML **文本内容**转义: 只转 & < > —— 与 TMM 一致。

    TMM 输出的文本里 `"` 与 `'` 是**原样**的(如 <role>A-Tuo's friend</role>),
    在 XML 文本节点里这样做完全合法,转成 &#x27; 反而与 TMM 不一致。
    """
    return (str(value).replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;"))


def fmt_dt(ts=None):
    """TMM 的 dateadded 格式: YYYY-MM-DD HH:MM:SS(本地时区)。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts else time.time()))


def country_zh(cc):
    """ISO 国家码 -> 中文名(未知码原样返回)。"""
    if not cc:
        return ""
    return COUNTRY_ZH.get(str(cc).upper(), str(cc).upper())


def lang_zh(code):
    """ISO 639 语言码 -> 中文名(未知码原样返回)。"""
    if not code:
        return ""
    return LANG_ZH.get(str(code).lower(), str(code).lower())


def normalize_source(value):
    """把各种写法(web-dl / WEB-DL / BluRay)规整成 TMM 的 <source> 取值。"""
    if not value:
        return ""
    key = re.sub(r"[\s_]+", "", str(value).upper())
    return SOURCE_MAP.get(key, key)


def _el(tag, value=None, indent=1, attrs=""):
    """生成一行元素;value 为 None/'' 时输出自闭合标签(与 TMM 一致)。"""
    pad = "  " * indent
    a = f" {attrs}" if attrs else ""
    if value is None or value == "":
        return f"{pad}<{tag}{a}/>"
    return f"{pad}<{tag}{a}>{xml_text(value)}</{tag}>"


def _el_if(tag, value, indent=1, attrs=""):
    """value 为空时**整行不输出**(TMM 对 actor/crew 里的 thumb 就是这么做的)。"""
    if value is None or value == "":  # noqa: SIM108
        return None
    return _el(tag, value, indent, attrs=attrs)


def _el_raw(tag, raw=None, indent=1, attrs=""):
    """value 已是 XML 片段(不转义)时用。"""
    pad = "  " * indent
    a = f" {attrs}" if attrs else ""
    if raw is None or raw == "":
        return f"{pad}<{tag}{a}/>"
    return f"{pad}<{tag}{a}>{raw}</{tag}>"


# ---------------------------------------------------------------------------
# wikidata id(按 IMDb id 反查)
# ---------------------------------------------------------------------------
_WD_CACHE = {}


def wikidata_id(imdb_id, timeout=25, cache_file=None):
    """按 IMDb id 反查 wikidata Q-id。失败返回 ''(不抛异常,不影响主流程)。"""
    if not imdb_id:
        return ""
    if imdb_id in _WD_CACHE:
        return _WD_CACHE[imdb_id]

    if cache_file:
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
            if imdb_id in cache:
                _WD_CACHE[imdb_id] = cache[imdb_id]
                return cache[imdb_id]
        except Exception:  # noqa: BLE001
            pass

    qid = ""
    try:
        q = f'SELECT ?item WHERE {{ ?item wdt:P345 "{imdb_id}" }} LIMIT 1'
        url = SPARQL_URL + "?" + urllib.parse.urlencode({"query": q, "format": "json"})
        req = urllib.request.Request(url, headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": "media-auto/1.0 (nfo generator)",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = (data.get("results") or {}).get("bindings") or []
        if rows:
            qid = (rows[0].get("item") or {}).get("value", "").rstrip("/").split("/")[-1]
    except Exception:  # noqa: BLE001
        qid = ""

    _WD_CACHE[imdb_id] = qid
    if cache_file:
        try:
            cache = {}
            try:
                with open(cache_file, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:  # noqa: BLE001
                cache = {}
            cache[imdb_id] = qid
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass
    return qid


# ---------------------------------------------------------------------------
# 电影 NFO
# ---------------------------------------------------------------------------
def build_movie_nfo(meta, info=None, *, source="", edition="NONE",
                    original_filename="", dateadded=None, wikidata="",
                    tmm_locked=True, cast_limit=0, tag_limit=60, crew_limit=40,
                    streamdetails=None):
    """生成 TMM 5.2.12 兼容的电影 NFO(XML 字符串)。

    meta : tmdb.detail_sync() 的返回
    info : mediainfo.probe_file() 的返回(可为 None,则不写 <fileinfo>)
    streamdetails : 也可直接传现成的 <streamdetails> XML 片段
    """
    title = meta.get("title") or "未知"
    orig = meta.get("originalTitle") or ""
    year = str(meta.get("year") or "")
    imdb = (meta.get("imdb_id") or "").strip()
    tmdb = meta.get("tmdb_id")
    coll = meta.get("collection") or None

    L = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
         f"<!--created on {fmt_dt(dateadded)} by {TMM_HEADER}-->",
         "<!--generated by MediaAuto(媒体自动化) - tinyMediaManager 兼容格式-->",
         "<movie>"]
    L.append(_el("title", title))
    L.append(_el("originaltitle", orig))
    L.append(_el("sorttitle"))
    L.append(_el("year", year))

    # --- ratings: 只有 themoviedb(IMDb 评分取不到),故给它 default="true" ---
    vote = meta.get("vote") or 0
    votes = meta.get("vote_count") or 0
    if vote:
        L.append("  <ratings>")
        L.append(f'    <rating default="true" max="10" name="themoviedb">')
        L.append(f"      <value>{vote:g}</value>")
        L.append(f"      <votes>{int(votes or 0)}</votes>")
        L.append("    </rating>")
        L.append("  </ratings>")
    else:
        L.append(_el("ratings"))
    L.append(_el("userrating", "0"))

    # --- set(合集): 拿不到时留空标签 ---
    if coll and coll.get("name"):
        L.append("  <set>")
        L.append(_el("name", coll.get("name"), 2))
        L.append(_el("overview", coll.get("overview"), 2))
        L.append("  </set>")
    else:
        L.append(_el("set"))

    plot = meta.get("overview") or ""
    L.append(_el("plot", plot))
    L.append(_el("outline", plot))
    L.append(_el("tagline", meta.get("tagline") or ""))
    if meta.get("runtime"):
        L.append(_el("runtime", str(int(meta["runtime"]))))
    else:
        L.append(_el("runtime"))
    cert = meta.get("certification") or ""
    L.append(_el("mpaa", cert))
    L.append(_el("certification", cert))
    L.append(_el("id", imdb or (f"tmdb{tmdb}" if tmdb else "")))
    L.append(_el("tmdbid", str(tmdb) if tmdb else ""))

    # --- uniqueid: 有 imdb 就 imdb 默认,否则 tmdb 默认 ---
    if tmdb:
        L.append(_el("uniqueid", str(tmdb), 1,
                     attrs=f'default="{"false" if imdb else "true"}" type="tmdb"'))
    if imdb:
        L.append(_el("uniqueid", imdb, 1, attrs='default="true" type="imdb"'))
    if coll and coll.get("id"):
        L.append(_el("uniqueid", str(coll["id"]), 1, attrs='default="false" type="tmdbSet"'))
    if wikidata:
        L.append(_el("uniqueid", wikidata, 1, attrs='default="false" type="wikidata"'))

    for cc in (meta.get("countries") or []):
        L.append(_el("country", country_zh(cc)))
    L.append(_el("premiered", (meta.get("premiered") or "")[:10]))
    L.append(_el("watched", "false"))
    L.append(_el("playcount", "0"))
    for g in (meta.get("genres") or []):
        L.append(_el("genre", g))
    for s in (meta.get("studios") or []):
        L.append(_el("studio", s))
    for d in (meta.get("directors") or []):
        attr = f'tmdbid="{d["tmdbid"]}"' if d.get("tmdbid") else ""
        L.append(_el("director", d.get("name"), 1, attrs=attr))
    for t in (meta.get("keywords") or [])[:tag_limit]:
        L.append(_el("tag", t))

    cast = meta.get("cast") or []
    for c in (cast[:cast_limit] if cast_limit else cast):
        _append_person(L, "actor", c, role_value=c.get("role"))

    _append_producers(L, meta)

    # --- trailer: 有 YouTube 链接就写;拿不到就自闭合(与 TMM 一致) ---
    L.append(_el("trailer", meta.get("trailer") or ""))

    langs = [lang_zh(x) for x in (meta.get("languages") or [])]
    for lg in [x for x in langs if x]:
        L.append(_el("languages", lg))
    L.append(_el("dateadded", fmt_dt(dateadded)))

    # --- fileinfo/streamdetails ---
    if streamdetails is None and info:
        from lib import mediainfo as _mi
        # streamdetails_xml 返回的就是完整 <fileinfo> 块,不要再包一层
        streamdetails = _mi.streamdetails_xml(info, indent="  ")
    if streamdetails:
        if streamdetails.lstrip().startswith("<fileinfo"):
            L.append(streamdetails)
        else:
            L.append("  <fileinfo>")
            L.append(streamdetails)
            L.append("  </fileinfo>")
    else:
        L.append(_el("fileinfo"))

    if coll and coll.get("id"):
        L.append(_el("collectionnumber", str(coll["id"])))

    # --- TMM meta data ---
    L.append("  <!--tinyMediaManager meta data-->")
    # ⚠️ 片源识别不出就留空(<source/>),**绝不硬填 WEBDL** ——
    # 旧逻辑 `normalize_source(source) or "WEBDL"` 会把"看不出片源"的条目标成 WEB-DL,
    # 这是错误信息(一个 Remux/BluRay/无标记的文件被写成 WEBDL)。宁缺毋滥:留空让
    # TMM/Jellyfin 后续自己判断,也不误导。
    L.append(_el("source", normalize_source(source)))
    L.append(_el("edition", edition or "NONE"))
    L.append(_el("original_filename", original_filename))
    L.append(_el("user_note"))
    L.append(_el("english_title", _english_title(meta)))
    _append_crew(L, meta, crew_limit)
    if tmm_locked:
        L.append(_el("tmm_locked"))
    L.append("</movie>")
    return "\n".join(L) + "\n"


def _english_title(meta):
    """english_title = 条目在 TMDB 的英文标题(TMM 就是取这个)。

    优先用 meta['english_title'](由 tmdb.english_title_sync 以 ?language=en 取回,
    如 等一个人咖啡 -> 'Café. Waiting. Love'、韶华若锦 -> 'Youthful Glory')。
    取不到时退回: 原文标题本身就是拉丁文字就用它,否则留空(不编造翻译)。
    """
    en = (meta.get("english_title") or "").strip()
    if en:
        return en
    orig = (meta.get("originalTitle") or "").strip()
    if orig and not re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", orig):
        return orig
    return ""


def _append_producers(lines, meta, limit=0):
    """独立 <producer tmdbid=".."> 块(注意: 与 <crew> 里的制片条目是两套, TMM 会同时写)。

    TMM 实测结构:
      <producer tmdbid="1012319">
        <name>九把刀</name>
        <role>Producer</role>
        <thumb>..</thumb>
        <profile>https://www.themoviedb.org/person/1012319</profile>
      </producer>
    与 <actor> 的差别: id 作属性而非子元素;`<role>` 放的是 job 名而不是角色名。
    """
    producers = meta.get("producers") or []
    for p in (producers[:limit] if limit else producers):
        attr = f'tmdbid="{p["tmdbid"]}"' if p.get("tmdbid") else ""
        _append_person(lines, "producer", p, open_attrs=attr,
                       role_value=p.get("role") or "Producer",
                       with_tmdbid_child=False)


def _el_open(tag, indent=1, attrs=""):
    """只输出开标签(用于需要子元素的块)。"""
    pad = "  " * indent
    a = f" {attrs}" if attrs else ""
    return f"{pad}<{tag}{a}>"


def _append_person(lines, tag, item, *, indent=1, open_attrs="", role_value=None,
                   role_attrs="", with_tmdbid_child=True):
    """写一个 <actor>/<crew>/<producer> 块。

    要点(按 TMM 实测输出对齐):
      - `<name>` 一定写;
      - `<role>` / `<thumb>` / `<profile>` / `<tmdbid>` **为空时整行省略**,而不是写成 <thumb/>;
        (TMM 对没有头像的演员就是直接不写 thumb)
      - `<thumb>` 是人物头像,`<profile>` 是 TMDB 人物主页链接。
    """
    kid = indent + 1
    lines.append(_el_open(tag, indent, attrs=open_attrs))
    rows = [
        _el("name", item.get("name"), kid),
        _el_if("role", role_value, kid, attrs=role_attrs),
        _el_if("thumb", item.get("thumb"), kid),
        _el_if("profile", item.get("profile"), kid),
    ]
    if with_tmdbid_child:
        rows.append(_el_if("tmdbid", str(item["tmdbid"]) if item.get("tmdbid") else "", kid))
    lines.extend(r for r in rows if r)
    lines.append("  " * indent + f"</{tag}>")


def _append_crew(lines, meta, crew_limit):
    """末尾统一 <crew><name><role subrole="Job">JOB</role><thumb><profile><tmdbid>。

    组成与顺序按 TMM 实测对齐: **制片人(保持 TMDB 顺序) + 导演**,
    **不含** Writer/Screenplay/Novel 等。
    (等一个人咖啡 的 TMM 输出正是: 九把刀 Producer / 柴智屏 Producer / Ching-Lin Chiang Director)
    """
    members = list(meta.get("producers") or []) + list(meta.get("directors") or [])
    members.sort(key=lambda x: x.get("tmdbid") or 0)   # TMM 按人物 id 升序
    for c in (members[:crew_limit] if crew_limit else members):
        job = c.get("job") or c.get("role") or ""
        if not job or not c.get("name"):
            continue
        _append_person(lines, "crew", c,
                       role_value=job.upper(),
                       role_attrs=f'subrole="{xml_esc(job)}"')


# ---------------------------------------------------------------------------
# 剧集 NFO
# ---------------------------------------------------------------------------
def build_tvshow_nfo(meta, *, wikidata="", dateadded=None, tmm_locked=True,
                     cast_limit=0, tag_limit=60, original_filename=""):
    """生成 TMM 5.2.12 兼容的 tvshow.nfo(XML 字符串)。

    注: TMM 的 tvshow.nfo **不写** <producer>/<crew>/<original_filename>/<fileinfo>,
        只到 <trailer> + <dateadded> + <enddate>,随后就是 meta data 段。
        (original_filename 参数保留仅为向后兼容,不再输出。)
    """
    title = meta.get("title") or "未知"
    orig = meta.get("originalTitle") or ""
    imdb = (meta.get("imdb_id") or "").strip()
    tmdb = meta.get("tmdb_id")
    tvdb = meta.get("tvdb_id")

    L = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
         f"<!--created on {fmt_dt(dateadded)} by {TMM_HEADER}-->",
         "<!--generated by MediaAuto(媒体自动化) - tinyMediaManager 兼容格式-->",
         "<tvshow>"]
    L.append(_el("title", title))
    L.append(_el("originaltitle", orig))
    L.append(_el("showtitle", title))
    L.append(_el("sorttitle"))
    L.append(_el("year", str(meta.get("year") or "")))

    vote = meta.get("vote") or 0
    votes = meta.get("vote_count") or 0
    if vote:
        L.append("  <ratings>")
        L.append('    <rating default="true" max="10" name="themoviedb">')
        L.append(f"      <value>{vote:g}</value>")
        L.append(f"      <votes>{int(votes or 0)}</votes>")
        L.append("    </rating>")
        L.append("  </ratings>")
    else:
        L.append(_el("ratings"))

    L.append(_el("userrating", "0"))
    L.append(_el("outline"))
    L.append(_el("plot", meta.get("overview") or ""))
    L.append(_el("tagline", meta.get("tagline") or ""))
    if meta.get("runtime"):
        L.append(_el("runtime", str(int(meta["runtime"]))))
    else:
        L.append(_el("runtime"))

    for s in (meta.get("seasons") or []):
        if s.get("number") is None or not s.get("name"):
            continue
        L.append(_el("namedseason", s.get("name"), 1, attrs=f'number="{int(s["number"])}"'))

    cert = meta.get("certification") or ""
    L.append(_el("mpaa", cert))
    L.append(_el("certification", cert))

    # --- episodeguide: TMM 用它把多源 id 一起写进去 ---
    # 注意: TMM 原样写 JSON(引号不转义) —— 在 XML 文本里 `"` 合法, 照抄以保持一致
    guide = {}
    if tmdb:
        guide["tmdb"] = str(tmdb)
    if imdb:
        guide["imdb"] = imdb
    if wikidata:
        guide["wikidata"] = wikidata
    if tvdb:
        guide["tvdb"] = str(tvdb)
    L.append(_el_raw("episodeguide", json.dumps(guide, separators=(",", ":")) if guide else ""))

    L.append(_el("id", str(tvdb) if tvdb else (imdb or (f"tmdb{tmdb}" if tmdb else ""))))
    L.append(_el("imdbid", imdb))
    L.append(_el("tmdbid", str(tmdb) if tmdb else ""))
    if tmdb:
        L.append(_el("uniqueid", str(tmdb), 1, attrs='default="false" type="tmdb"'))
    if imdb:
        L.append(_el("uniqueid", imdb, 1, attrs='default="false" type="imdb"'))
    if wikidata:
        L.append(_el("uniqueid", wikidata, 1, attrs='default="false" type="wikidata"'))
    if tvdb:
        L.append(_el("uniqueid", str(tvdb), 1, attrs='default="true" type="tvdb"'))

    L.append(_el("premiered", (meta.get("premiered") or "")[:10]))
    L.append(_el("status", meta.get("status") or ""))
    L.append(_el("watched", "false"))
    L.append(_el("playcount"))
    for g in (meta.get("genres") or []):
        L.append(_el("genre", g))
    for s in (meta.get("studios") or []):
        L.append(_el("studio", s))
    for cc in (meta.get("countries") or []):
        L.append(_el("country", country_zh(cc)))
    for t in (meta.get("keywords") or [])[:tag_limit]:
        L.append(_el("tag", t))

    cast = meta.get("cast") or []
    for c in (cast[:cast_limit] if cast_limit else cast):
        _append_person(L, "actor", c, role_value=c.get("role"))

    L.append(_el("trailer", meta.get("trailer") or ""))
    L.append(_el("dateadded", fmt_dt(dateadded)))
    if meta.get("end_date"):
        L.append(_el("enddate", (meta["end_date"] or "")[:10]))

    L.append("  <!--tinyMediaManager meta data-->")
    L.append(_el("user_note"))
    L.append("  <episode_groups>")
    L.append('    <group active="true" id="AIRED" name=""/>')
    L.append("  </episode_groups>")
    L.append(_el("english_title", _english_title(meta)))
    if tmm_locked:
        L.append(_el("tmm_locked"))
    L.append("</tvshow>")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    demo_movie = {
        "title": "等一个人咖啡", "originalTitle": "等一個人咖啡", "year": "2014",
        "imdb_id": "tt3974790", "tmdb_id": 287703, "tvdb_id": None,
        "tagline": "", "runtime": 119, "premiered": "2014-08-15",
        "vote": 6.3, "vote_count": 45, "certification": "HK:IIA",
        "countries": ["HK", "TW"], "languages": ["zh"],
        "studios": ["Edko Films", "Amazing Film Studio"],
        "genres": ["喜剧", "爱情", "奇幻"],
        "keywords": ["friendship", "supernatural"],
        "directors": [{"name": "Ching-Lin Chiang", "tmdbid": 1354894}],
        "cast": [{"name": "宋芸桦", "role": "Lee Siying", "tmdbid": 1354895,
                  "profile": "https://www.themoviedb.org/person/1354895",
                  "thumb": "https://image.tmdb.org/t/p/h632/x.jpg"}],
        "crew": [{"name": "九把刀", "job": "Producer", "tmdbid": 1012319,
                  "profile": "p", "thumb": "t"}],
        "collection": None, "seasons": [], "overview": "剧情简介 & 测试 <转义>",
    }
    sd = ("    <streamdetails>\n"
          "      <video>\n        <codec>hevc</codec>\n        <aspect>2.40</aspect>\n"
          "        <width>1920</width>\n        <height>808</height>\n"
          "        <resolution>1080</resolution>\n"
          "        <durationinseconds>7137</durationinseconds>\n      </video>\n"
          "    </streamdetails>")
    out = build_movie_nfo(demo_movie, streamdetails=sd, source="BLURAY",
                          original_filename="Cafe.Waiting.Love.mkv",
                          dateadded=1757878927, wikidata="Q16077910")
    print(out)
    print("--- 转义检查: 应出现 &amp; 与 &lt;转义&gt; ---")
    print("  &amp; 存在:", "&amp;" in out, "| &lt;转义&gt; 存在:", "&lt;转义&gt;" in out)

    demo_tv = dict(demo_movie, title="韶华若锦", originalTitle="韶华若锦",
                   year="2025", imdb_id="tt32221618", tmdb_id=262741, tvdb_id=463856,
                   runtime=47, premiered="2025-05-19", end_date="2025-06-02",
                   status="Ended", certification="", countries=["CN"], languages=["zh"],
                   studios=["Mango TV", "MangoTV"], genres=["剧情"],
                   seasons=[{"number": 1, "name": "第 1 季", "episodes": 30}],
                   directors=[], crew=[])
    print()
    print(build_tvshow_nfo(demo_tv, wikidata="Q134892123", dateadded=1757392050))
