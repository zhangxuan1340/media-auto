#!/usr/bin/env python3
"""
命名与广告识别(纯函数,无副作用,可单测)
=========================================
处理"高清影视之家"这类种子/目录名里的推广信息,并生成规范的媒体库目录名。

涉及三类名字:
  1) 种子/目录名(带广告):  【高清影视之家发布 www.HDBTHD.com】误杀2[60帧率版本][高码版][国语配音+中文字幕].Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD
  2) 广告文件(整名就是推广):  【更多无水印蓝光原盘请访问 www.HDBTHD.com】【更多无水印蓝光原盘请访问 www.HDBTHD.com】.MP4
  3) 目标目录名(规范):        误杀2.2021.tt16117346   /   误杀2.2021.tmdb899665(无 imdb 时)

判定广告文件的思路(关键):
  把文件名里的【推广块】全部剥掉后,如果【剩下没有任何实际片名】,它就是广告文件。
  - 【更多无水印蓝光原盘请访问 www.HDBTHD.com】【…】.MP4  → 剥完是空  → 广告
  - 【高清影视之家发布 www.HDBTHD.com】误杀2[...].Fireflies...-DreamHD → 剥完还有片名 → 媒体
  这条规则对"换域名/换文案/换扩展名"的变体天然免疫,不需要维护域名黑名单。
"""
import os
import re

# 文件名正则的边界: **不要用 \b** —— Python 的 \b 按 \w 判定, 而 \w 含 CJK,
# '中字S01E01' 里中文与 'S' 之间不算边界, `\bS\d+E\d+\b` 匹配不到 → 正片被判成
# 广告、季集抽不出来(2026-09-26 审查)。统一改成"前后不能是 ASCII 字母/数字/下划线"。
_LB = r"(?<![0-9A-Za-z_])"
_RB = r"(?![0-9A-Za-z_])"

# ---------------------------------------------------------------------------
# 扩展名分类(可在 管理 → 通用 → 整理 里覆盖)
# ---------------------------------------------------------------------------
# 视频(媒体): 保留
VIDEO_EXT = {
    ".mkv", ".mp4", ".ts", ".m2ts", ".mts", ".avi", ".mov", ".wmv", ".flv",
    ".rmvb", ".rm", ".mpg", ".mpeg", ".m4v", ".webm", ".vob", ".iso", ".3gp",
    ".divx", ".asf", ".f4v", ".ogm",
}
# 字幕: 保留(属于"媒体文件"的一部分,不删)
SUBTITLE_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi", ".lrc"}

# 非视频杂项: 删除(用户已确认"顺带删非视频杂项")
# 保守起见不含 .pdf/.doc/.docx/.xls 等"可能是正片资料"的类型;需要时可加到 config。
# 视为「非视频杂项」(默认删除)的扩展名。
# 覆盖高清站常见的推广文件形态: 文档(下载必看.doc/.pdf/.ppt)、网页(更多资源.html)、
# 快捷方式/脚本(.url/.exe/.bat)、校验(.sfv/.md5)、种子、压缩包,以及截图/海报类图片。
DEFAULT_JUNK_EXT = {
    # 文档 / 文本 / 表格
    ".txt", ".md", ".rtf", ".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".odt", ".ods", ".odp", ".wps", ".wpt", ".et", ".ett", ".dps", ".dpt",
    # 网页 / 配置 / 数据
    ".url", ".lnk", ".html", ".htm", ".mht", ".mhtml", ".xml", ".json", ".ini", ".cfg",
    ".conf", ".db", ".log", ".dat", ".bak", ".tmp",
    # 可执行 / 脚本 / 安装包(下载站推广的典型载体,绝不保留)
    ".exe", ".bat", ".cmd", ".scr", ".vbs", ".js", ".jse", ".wsf", ".com", ".msi",
    ".apk", ".dmg", ".pkg", ".deb", ".rpm", ".jar",
    # 校验 / 种子
    ".sfv", ".md5", ".sha1", ".sha256", ".torrent",
    # 图片(广告图/截图;库内海报等成品资产另有 LIBRARY_ART_STEMS 白名单保护)
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".avif",
    # 压缩包
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".001",
}

# 媒体库成品资产: 这些名字/扩展名的文件【永不当作杂项删除】。
# 例如 TMM/Jellyfin 生成的 poster.jpg / fanart.jpg / movie.nfo / tvshow.nfo / theme.mp3。
LIBRARY_ART_STEMS = {
    "poster", "folder", "cover", "fanart", "background", "backdrop", "banner",
    "logo", "clearlogo", "clearart", "disc", "discart", "cdart", "thumb",
    "landscape", "keyart", "character", "season", "movie", "tvshow", "theme",
    "episode", "album", "artist", "series",
}
# 这些扩展名配合上面的名字才算成品资产
LIBRARY_ART_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".nfo", ".mp3", ".xml"}
# NFO 单独一桶: 只在「我们即将写入自己的 NFO」时才替换,否则保留(避免抹掉已有刮削结果)
NFO_EXT = {".nfo"}


def is_library_asset(name):
    """是否是媒体库成品资产(海报/背景图/已有 NFO 等),属于「绝不能删」。"""
    stem, ext = os.path.splitext(name or "")
    ext = ext.lower()
    if ext not in LIBRARY_ART_EXT:
        return False
    s = stem.lower().strip(" .-_")
    if ext in NFO_EXT:
        return True  # NFO 一律不在此处删(单独处理)
    for art in LIBRARY_ART_STEMS:
        if s == art or s.startswith(art) and (len(s) == len(art) or not s[len(art)].isalnum()):
            return True
    return False

# ---------------------------------------------------------------------------
# 推广信息识别
# ---------------------------------------------------------------------------
# 域名后缀分两档:
#   STRONG —— 几乎不可能作为普通英文单词出现在点分文件名里 → 裸域名也算"强证据"
#   WEAK   —— tv/me/la/im/io/pro 等可能是单词(Some.TV.Show),只在括号内才算推广
_STRONG_TLDS = "com|net|org|cc|xyz|top|vip|biz|site|club|online|cn|info|art|fun"
_WEAK_TLDS = "tv|me|la|im|io|pro|name|link|live"
_ALL_TLDS = _STRONG_TLDS + "|" + _WEAK_TLDS
# 括号内使用的域名特征(含弱后缀)
_DOMAIN_HINT = r"(?:www\.[\w-]+|https?://[\w./?=&\-]+|[\w-]+\.(?:" + _ALL_TLDS + r")" + _RB + ")"
# 裸域名(只用强后缀,避免误伤)
_BARE_HINT = r"(?:https?://[\w./?=&\-]+|www\.[\w.\-]+|[\w-]+\.(?:" + _STRONG_TLDS + r")" + _RB + ")"
# 括号(中/英/日式)包裹、且内含域名 → 推广块。括号内允许任意文案。
PROMO_BLOCK = re.compile(
    r"[【\[〔《〈(（][^】\]〕》〉)）]{0,200}?" + _DOMAIN_HINT + r"[^】\]〕》〉)）]{0,200}?[】\]〕》〉)）]",
    re.IGNORECASE,
)
# 不在括号里的裸域名/网址
BARE_DOMAIN = re.compile(_BARE_HINT, re.IGNORECASE)
# 任意括号块 + 该块内是否含域名
BLOCK = re.compile(r"[【\[〔《〈(（][^】\]〕》〉)）]*[】\]〕》〉)）]")
BLOCK_HAS_DOMAIN = re.compile(_DOMAIN_HINT, re.IGNORECASE)

# 纯技术标签(抽取标题时剔除)
TECH_TOKEN = re.compile(
    r"^(?:"
    r"\d{3,4}[pi]|4k|8k|uhd|hd|sd|fhd|hq|"
    r"x26[45]|h\.?26[45]|hevc|avc|10bit|8bit|12bit|hdr|hdr10|dolby|dv|vision|"
    r"web-?dl|web-?rip|webrip|blu-?ray|bdrip|brrip|dvdrip|hdrip|remux|"
    r"ddp?|dd\+|ac3|eac3|dts|dts-?hd|truehd|atmos|flac|aac|mp3|"
    r"\d+(?:\.\d+)?(?:ch|fps|kbps|mbps)|"
    r"internal|repack|proper|complete|"
    r"chinese|chs|cht|eng|jpn|kor|mandarin|cantonese|"
    r"国语|国配|粤语|中字|中文字幕|字幕|双语|简繁|简体|繁体|"
    r"高清|蓝光|原盘|无水印|修复版|重制版|加长版|导演剪辑版|"
    r"帧率|高码|高码版|版本"
    r")$",
    re.IGNORECASE,
)
# 发布组尾巴: -DreamHD / -BlackTV / -FLTTH
RELEASE_GROUP = re.compile(r"-[A-Za-z0-9_@]{2,20}$")
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
# 单独的季/集标记(拼查询时应去掉,如 S03 / E05)
_SEASON_TOKEN = re.compile(r"^S\d{1,2}$|^E\d{1,3}$|^EP?\d{1,3}$|^S\d{1,2}E\d{1,3}$", re.IGNORECASE)
_TAIL_NUM = re.compile(r"\d+$")
# "正片标记": 季集编号(正片几乎都有,广告不会有)
EP_MARKER = re.compile(
    rf"{_LB}S\d{{1,2}}E\d{{1,3}}{_RB}|{_LB}S\d{{1,2}}{_RB}|{_LB}E\d{{1,3}}{_RB}|{_LB}EP?\d{{1,3}}{_RB}"
    r"|第\s*\d+\s*[季集话話期]|共\s*\d+\s*[季集話]|全\s*\d+\s*[集話]",
    re.IGNORECASE,
)
# 文件系统非法字符(Windows/macOS 通用)
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def strip_promos(name):
    """剥掉推广块与裸域名,保留真实片名部分。

    - 括号内**含域名**的整块删除(如 【更多无水印…请访问 www.x.com】)
    - 括号内**不含域名**的块保留(如 [60帧率版本]、[国语配音+中文字幕])
    - 不在括号里的裸域名删除
    """
    if not name:
        return ""
    out = []
    pos = 0
    for m in BLOCK.finditer(name):
        if BLOCK_HAS_DOMAIN.search(m.group(0)):  # 该括号块是推广 → 丢掉
            out.append(name[pos:m.start()])
            out.append(" ")
            pos = m.end()
    out.append(name[pos:])
    s = "".join(out)
    s = BARE_DOMAIN.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ._-·|")


def is_promo_only(name):
    """整名就是推广信息(剥掉推广块后没有片名)→ 广告文件。

    注意用【原始名去掉扩展名】来判断,且把分隔符/括号都清掉后长度必须为 0。
    """
    stem = os.path.splitext(name or "")[0]
    s = strip_promos(stem)
    s = re.sub(r"[\s._\-·|—–]+", "", s)
    s = re.sub(r"[【】\[\]〔〕《》〈〉()（）{}<>]", "", s)
    return s == ""


def is_ad_file(name, size=0):
    """是否是广告文件。两条规则(满足任一即广告):

    R1 整名就是推广: 剥掉【含域名的推广块】后没有任何片名。
       例: 【更多无水印高清电影请访问 www.HDBTHD.com】【…】.MKV
    R2 名字里含域名,且【既没有年份、也没有季集编号】——正片几乎不会这样
       (正片名一般带 2021 / S01E02 / 第1季 / 全12集 之类标记)。
       例: 高清影视之家 www.hdbthd.com.mkv

    这两条对"换域名/换文案/换扩展名"的变体都成立,不需要维护域名黑名单。
    若某条规则误伤,可在 管理 → 通用 → 整理(organize.ad_disable)里关掉对应规则。
    """
    if not name:
        return False
    if is_promo_only(name):
        return True
    if BARE_DOMAIN.search(name) and not YEAR_RE.search(name) and not EP_MARKER.search(name):
        return True
    return False


def has_promo(name):
    """名字里是否带推广信息(用于报告/日志)。"""
    if not name:
        return False
    return bool(PROMO_BLOCK.search(name)) or bool(BARE_DOMAIN.search(name)) or is_promo_only(name)


def ext_of(name):
    return os.path.splitext(name or "")[1].lower()


def classify_file(name, size=0, junk_ext=None, video_ext=None, subtitle_ext=None, ad_disable=None):
    """给一个文件分类,返回 'ad' | 'junk' | 'nfo' | 'asset' | 'subtitle' | 'media' | 'other'。

    顺序很重要:
      1) 先判"是不是广告" —— 这样 289KB 的 【…www.x.com…】.MKV 会判成 ad 而不是 media;
      2) 再看是不是「库内成品资产」(poster.jpg / fanart.jpg / 已有 .nfo) —— 这类永不删;
      3) 最后按扩展名分到 junk / subtitle / media。
    ad_disable: 传 {'promo_only','domain_no_year'} 的子集可关闭对应广告规则。
    """
    video_ext = video_ext if video_ext is not None else VIDEO_EXT
    subtitle_ext = subtitle_ext if subtitle_ext is not None else SUBTITLE_EXT
    junk_ext = junk_ext if junk_ext is not None else DEFAULT_JUNK_EXT
    off = set(ad_disable or ())

    is_ad = False
    if "promo_only" not in off and is_promo_only(name):
        is_ad = True
    elif ("domain_no_year" not in off and BARE_DOMAIN.search(name or "")
          and not YEAR_RE.search(name or "") and not EP_MARKER.search(name or "")):
        is_ad = True
    if is_ad:
        return "ad"

    e = ext_of(name)
    # 库内成品资产优先保护(海报/背景图等) —— 不能被下面的 junk_ext 吃掉
    if e in NFO_EXT:
        return "nfo"
    if is_library_asset(name) and e not in video_ext and e not in subtitle_ext:
        return "asset"
    if e in junk_ext:
        return "junk"
    if e in subtitle_ext:
        return "subtitle"
    if e in video_ext:
        return "media"
    return "other"


# ---------------------------------------------------------------------------
# 从种子名/目录名抽取标题与年份
# ---------------------------------------------------------------------------
def clean_for_title(name):
    """把种子名清洗成"标题 + 技术标签"的形态,便于抽取标题与年份。"""
    n = strip_promos(name or "")
    n = BLOCK.sub(" ", n)              # 去掉所有括号块([60帧率版本]/[国语配音+中文字幕] 等)
    # ⚠️ 不能用 os.path.splitext 直接剥尾: "玩具总动员.1995" 会被当成扩展名把年份吃掉,
    # 导致 extract_title_year 抽不到年份 → 整个年份层级消歧失效(同名/系列片只能靠 score
    # 竞争、按 DB 顺序取第一个)。只剥"真扩展名"(字母结尾),纯数字后缀(年份/序号)保留。
    _root, _ext = os.path.splitext(n)
    if not (_ext and _ext[1:].isdigit()):
        n = _root
    n = RELEASE_GROUP.sub("", n)       # 去掉发布组尾 -DreamHD
    n = n.replace("_", ".").replace("-", " ")
    n = re.sub(r"\s{2,}", " ", n)
    return n.strip(" .")


def extract_title_year(name):
    """从种子/目录名抽取 (标题主体, 年份)。

    返回 title 是"年份之前的连续片段"(可能中英混排),year 为 4 位数字或 ''。
    例: 【…www.HDBTHD.com】误杀2[60帧率版本][高码版][国语配音+中文字幕].Fireflies.in.the.Sun.2021.2160p...
        -> ('误杀2 Fireflies in the Sun', '2021')
    """
    n = clean_for_title(name)
    year = ""
    m = YEAR_RE.search(n)
    head = n
    if m:
        year = m.group(1)
        head = n[:m.start()]
    tokens = [t for t in re.split(r"[.\s]+", head) if t]
    tokens = [t for t in tokens if not TECH_TOKEN.match(t)]
    title = " ".join(tokens).strip(" .")
    return title, year


def title_query_candidates(name, title=None, year=None):
    """给出若干"喂给搜索接口"的候选查询,按可信度从高到低。

    经验(实测):
      - **不要给查询强行加年份**。"Fireflies in the Sun 2021" 搜不到,而裸的
        "Fireflies in the Sun" 能精准命中《误杀2》。年份交给 resolve 阶段做排序打分。
      - 中文名优先(中文命中很准);再是去掉结尾数字的形态
        (《隋唐英雄3》搜不到,但《隋唐英雄》能命中);最后才是英文部分。
    """
    if title is None or year is None:
        title, year = extract_title_year(name)
    toks = [t for t in re.split(r"[.\s]+", title or "") if t]
    if not toks:
        base = strip_promos(os.path.splitext(name or "")[0])
        return [base] if base else []

    cjk = [t for t in toks if re.search(r"[\u4e00-\u9fff]", t)]
    latin = [t for t in toks
             if not re.search(r"[\u4e00-\u9fff]", t) and not _SEASON_TOKEN.match(t)]

    cands = []
    if cjk:
        cands.append(cjk[0])                            # 首个中文词(裸)
        if len(cjk) > 1:
            cands.append(" ".join(cjk))                 # 全部中文词
        stripped = _TAIL_NUM.sub("", cjk[0])            # 去掉结尾数字: 隋唐英雄3 -> 隋唐英雄
        if stripped and stripped != cjk[0]:
            cands.append(stripped)
    if latin:
        cands.append(" ".join(latin))                   # 英文部分(去掉季集标记)
    cands.append(title.strip())                         # 年份前整体
    if year:                                            # 带年份兜底
        cands.append(f"{cjk[0]} {year}" if cjk else f"{' '.join(latin)} {year}")

    seen, out = set(), []
    for c in cands:
        c = (c or "").strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 目标目录名
# ---------------------------------------------------------------------------
def sanitize(text):
    """清理成安全的目录/文件名(去掉非法字符与首尾点/空格)。"""
    s = _ILLEGAL.sub(" ", text or "")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s.strip(" .")


def build_folder_name(title, year="", imdb_id="", tmdb_id=""):
    """生成规范目录名: title.year.ttXXXXXXX,无 imdb 时用 tmdb<id>。

    例: ('误杀2', '2021', 'tt16117346', 899665) -> '误杀2.2021.tt16117346'
        ('误杀2', '2021', '',          899665) -> '误杀2.2021.tmdb899665'
    年份/imdb/tmdb 缺失时自动省略对应段,不会留下多余的点。
    """
    parts = [sanitize(title or "")]
    if year:
        parts.append(str(year))
    ib = (imdb_id or "").strip()
    if ib:
        if not ib.startswith("tt"):
            ib = "tt" + ib.lstrip("t")
        parts.append(ib)
    elif tmdb_id:
        parts.append(f"tmdb{tmdb_id}")
    parts = [p for p in parts if p]
    return sanitize(".".join(parts)) or "未命名"


# 已规范化的目录名: <任意标题>.<YYYY>.tt1234567  或  <任意标题>.<YYYY>.tmdb12345
NORMALIZED_RE = re.compile(r"^.+[.\s](?:19\d{2}|20\d{2})[.\s](?:tt\d{6,}|tmdb\d+)$", re.IGNORECASE)
# TMM 默认风格目录名: 标题 (年份)  例: 指环王3：王者无敌 (2003) / 保持沉默 (2019)
TITLE_YEAR_RE = re.compile(r"^.+\s*[\(（](?:19\d{2}|20\d{2})[\)）]$")


def render_template(template, **fields):
    """渲染命名模板,如 '{title} ({year}) {quality}'。

    缺失字段按空串处理,并清掉因此产生的空括号/多余空格与末尾分隔符 ——
    这样没年份时 '误杀2 (2021)' 会自然退化成 '误杀2',不会留下 '误杀2 ()'。
    """
    out = str(template or "")
    for key, val in fields.items():
        out = out.replace("{" + key + "}", str(val if val is not None else "").strip())
    out = re.sub(r"[\(\[（【]\s*[\)\]）】]", "", out)      # 空括号
    out = re.sub(r"\s{2,}", " ", out)
    # 空格紧邻「点/下划线」分隔符时去掉空格(如 '误杀2 .2021' → '误杀2.2021')。
    # ⚠️ 不能把连字符 - 算进这里: 剧集模板 '{title} - {seasonep} - {quality}' 的
    # ' - ' 是【有意】的空格包裹分隔符(库内惯例 '标题 - S01E01 - 1080p'),
    # 若去掉 dash 前空格会变成 '标题-S01E01- 1080p', 与全库 200+ 文件不一致。
    # 连字符的去重仍由下一行处理(连续多个 - 折叠成一个), 不影响单侧空格。
    out = re.sub(r"\s+([.\_])", r"\1", out)               # 空格紧邻点/下划线分隔符
    out = re.sub(r"([.\-_]){2,}", r"\1", out)             # 连续分隔符折叠
    return sanitize(out.strip(" .-_"))


def name_fields(meta, quality=""):
    """把元数据摊平成模板字段(title/year/imdb/tmdb/quality/kind...)。"""
    meta = meta or {}
    tmdb = meta.get("tmdb_id")
    imdb = (meta.get("imdb_id") or "").strip()
    return {
        "title": meta.get("title") or meta.get("_fallback_title") or "",
        "original": meta.get("originalTitle") or "",
        "year": str(meta.get("year") or meta.get("detected_year") or ""),
        "imdb": imdb,
        "tmdb": f"tmdb{tmdb}" if tmdb else "",
        "tmdbid": str(tmdb or ""),
        "kind": meta.get("kind") or "",
        "quality": quality or "",
    }


def folder_name_from_meta(meta, template="{title} ({year})", quality=""):
    """按模板生成目录名。默认 TMM 风格 '标题 (年份)'。"""
    return render_template(template, **name_fields(meta, quality)) or "未命名"


def is_normalized(name):
    """判断目录名是否已是成品形式(用于判定"已整理过",跳过反查)。

    两种都算:
      - title.year.tt1234567 / title.year.tmdb12345  (早期 MediaAuto 风格)
      - 标题 (年份)                                   (TMM 默认风格)
    """
    s = sanitize(name or "")
    return bool(NORMALIZED_RE.match(s) or TITLE_YEAR_RE.match(s))


# ---------------------------------------------------------------------------
# 剧集季集解析 + 集文件命名(organize 按季归位 / 标准改名用)
# ---------------------------------------------------------------------------
# SxxExx / Sxx.E01 / Sxx_E01 / Sxx-E01 / Sxx E01 / Sxx x yy 等主流写法
# ⚠️ 季与集之间的分隔符是 [.\s_\-]* —— 点/下划线/连字符/空白都算(2026-09-20 修复:
# 旧版只写 \s*, 导致 S01.E33 这种【点分格式】解析不出集号、被误判成整季包 S01,
# 整季包改名又撞名 → 集文件全部保留原名未规范(用户反馈"爱的理想生活 S01.E33 没改名")。
_SEASON_EP_RE = re.compile(_LB + r"S(\d{1,2})[.\s_\-]*E(\d{1,3})" + _RB, re.IGNORECASE)
_SEASON_EP_X_RE = re.compile(_LB + r"S(\d{1,2})[.\s_\-]*[xX×][.\s_\-]*E?(\d{1,3})(?!\d)", re.IGNORECASE)
# 裸 Sxx(整季包: 只有季没有集, 如 Show.S01.1080p.mkv)
# ⚠️ 用 (?!\d) 防 "S012" 误读; 不要求 \b 在 E 侧, 因为 S02E01 已由上面的规则先吃掉
_SEASON_ONLY_RE = re.compile(_LB + r"S(\d{1,2})(?!\d)", re.IGNORECASE)

# 中文数字(季/集号): 兼容 1~几十 的阿拉伯与中文写法
_CN_NUM_RE = r"[\d一二两三四五六七八九十]+"
# 中文"第N季"(季号显式标记, N 可阿拉伯或中文数字)
_SEASON_CHINESE_RE = re.compile(r"第\s*(" + _CN_NUM_RE + r")\s*季")
# 中文"第N集/話/话"(N 可阿拉伯或中文数字)
_EP_CHINESE_RE = re.compile(r"第\s*(" + _CN_NUM_RE + r")\s*[集話话]")
# 季子目录名识别(已有结构化种子里的 Season 01 / Specials / 第N季)
_SEASON_DIR_PART_RE = re.compile(r"^(?:season\s*(\d+)|specials)$", re.IGNORECASE)
_SEASON_DIR_CN_RE = re.compile(r"^第\s*(" + _CN_NUM_RE + r")\s*季$")


def _cn_num_to_int(s):
    """中文/阿拉伯数字串 → int;解析不出返回 None。支持 一~九、十、十五、二十、二十一、3。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s == "十":
        return 10
    if "十" in s:
        tens_part, ones_part = s.split("十", 1)
        tens = digits.get(tens_part, 1) if tens_part else 1  # "十五"→ tens 空 → 1
        ones = digits.get(ones_part, 0) if ones_part else 0
        return tens * 10 + ones
    return digits.get(s)


def parse_episode(name, default_season=None):
    """从文件名/目录名抽取 (season, episode)。

    优先级: SxxExx > 中文第N季(+第M集) > 中文第M集 > 裸Sxx(整季包) > 中文第N季(整季包)。

    ⚠️ **季号没有显式标记时返回 (None, ep),绝不默认填 1** ——
    旧逻辑把"第N集"在找不到季号时默认 season=1,会把【第三季】的"第3季第5集"
    错塞进 Season 1(错误兜底,无声破坏剧集结构)。现在季号不明就返回 None,
    由调用方回退(所在目录名 / 保留原地),宁可留原地也不猜错季。
    """
    if not name:
        return None, None
    m = _SEASON_EP_RE.search(name) or _SEASON_EP_X_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 中文 第N季(显式季号)
    cs = _SEASON_CHINESE_RE.search(name)
    season = _cn_num_to_int(cs.group(1)) if cs else None
    # 中文 第M集(季号须上面已有显式标记, 否则保持 None)
    ce = _EP_CHINESE_RE.search(name)
    if ce:
        ep = _cn_num_to_int(ce.group(1))
        if ep is not None:
            return season, ep
    # 裸 Sxx(整季包)
    m = _SEASON_ONLY_RE.search(name)
    if m:
        return int(m.group(1)), None
    # 中文 第N季(整季包, 无集号)
    if season is not None:
        return season, None
    return None, None


def season_of_dirname(dirname):
    """子目录名里的季号提示: 'Season 1'→1, 'Specials'→0, '第3季'→3, 其它→None。"""
    s = (dirname or "").strip()
    m = _SEASON_DIR_PART_RE.match(s)
    if m:
        return int(m.group(1)) if m.group(1) is not None else 0
    m = _SEASON_DIR_CN_RE.match(s)
    if m:
        return _cn_num_to_int(m.group(1))
    return None


def season_dir_name(season):
    """季子目录名,对齐库内现有惯例(Jellyfin/TMM): 'Season 1' / 'Specials'(第0季)。

    ⚠️ 不补零 —— 现库全部是 'Season 1'(无零填充),补零会造成同剧两个季目录。
    """
    if season == 0:
        return "Specials"
    return f"Season {season}"


# 集文件名默认模板(与库内现有风格一致: '标题 - S01E01 - 1080p h265 AC3')
TV_FILE_TEMPLATE = "{title} - {seasonep} - {quality}"


def tv_episode_filename(title, season, episode, quality="", template=TV_FILE_TEMPLATE):
    """集文件名(不含扩展名)。占位符: {title} {seasonep}(S01E01 / S01) {season} {episode} {quality}。

    整季包(episode=None)→ seasonep='S01' → '标题 - S01 - 质量'。
    """
    seasonep = f"S{season:02d}E{episode:02d}" if episode is not None else f"S{season:02d}"
    return render_template(
        template,
        title=title or "未命名", seasonep=seasonep,
        season=f"S{season:02d}", episode=f"E{episode:02d}" if episode is not None else "",
        quality=quality or "")


# ---------------------------------------------------------------------------
# 反查结果的置信度校验(防止模糊匹配错条目)
# ---------------------------------------------------------------------------
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9']{1,}")


def is_cjk(t):
    """是否含 CJK 字符(中日韩, 含假名/韩文) —— 全库唯一实现。

    用于判断片名/原始语言的文字体系(calibrate 的 _is_cjk 委托到这里)。
    注意与 _CJK_RUN 不同: 这里连假名/韩文也算, 语义是"东亚文字体系"。
    """
    return any("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"
               or "\uac00" <= ch <= "\ud7af" for ch in (t or ""))
# 英文虚词不参与比对(它们在标题里几乎无信息量)
_LATIN_STOP = {
    "the", "of", "a", "an", "and", "in", "on", "at", "to", "for", "with",
    "part", "aka", "le", "la", "les", "el", "de", "die", "das", "il", "un",
}


def _cjk_runs(s):
    return [r for r in _CJK_RUN.findall(s or "") if len(r) >= 2]


def _latin_tokens(s):
    return {t.lower() for t in _LATIN_TOKEN.findall(s or "")} - _LATIN_STOP


def title_match(title, source):
    """反查到的片名 与 原始种子名/目录名 的匹配度,返回 0.0~1.0。

    用于挡住模糊匹配的错条目(例: 目录名「国安…」被搜成《国土安全》;
    短名《长安》套上长剧名《长安十二时辰》)。
      - 中文: 对标题里每个中文主干 run(>=2 字连续),看 source 里是否有 run【容纳】它:
          · source 有等长 run 容纳 → 该 run 满分 1.0(官方名==目录主干,如 '风起洛阳'→'风起洛阳 S01')
          · source 只有【更长】run 容纳 → 短名套长名,按比例给分;短主干(<4 字)套更长 → 直接 0
          · source 完全容纳不了该 run → 0(如 '国土安全' vs '国安')
        取各 run 的最低分(宁严勿宽,有一个主干套长名就压低整体)。
        ⚠️ 分母只用"容纳它的那个 run 的长度",**不含目录名里 '第一季/中文字幕/国语中字'
           等其它 CJK 词** —— 否则正确匹配会被非标题词拉低(误伤)。
      - 英文: 标题的实词 token 被来源覆盖的比例(分母取标题侧,宁严勿宽)。
    """
    if not title or not source:
        return 0.0
    title_runs = _cjk_runs(title)
    if title_runs:
        src_runs = _cjk_runs(source)
        ratios = []
        for tr in title_runs:
            placed = False
            for sr in src_runs:
                if tr in sr:
                    placed = True
                    if len(sr) > len(tr):
                        # 短名套长名: 短主干套更长 run → 拒;否则按比例
                        if len(tr) < 4:
                            return 0.0
                        ratios.append(len(tr) / len(sr))
                    else:
                        ratios.append(1.0)
                    break
            if not placed:
                return 0.0  # 标题某主干 run 在 source 里找不到 → 不匹配
        return min(ratios)
    tt, st = _latin_tokens(title), _latin_tokens(source)
    if tt and st:
        return len(tt & st) / len(tt)
    return 0.0


def best_title_match(meta, names):
    """在候选片名(中文名/原名)与若干来源名之间取最高匹配度。"""
    titles = [meta.get("title"), meta.get("originalTitle") or meta.get("original_title")]
    best = 0.0
    for t in titles:
        for n in names:
            best = max(best, title_match(t, n))
    return best


# ---------------------------------------------------------------------------
# 自测: python3 lib/naming.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ADS = [
        # 用户实测的三个广告文件
        "【更多无水印蓝光原盘请访问 www.HDBTHD.com】【更多无水印蓝光原盘请访问 www.HDBTHD.com】.MP4",
        "【更多无水印蓝光电影请访问 www.HDBTHD.com】【更多无水印蓝光电影请访问 www.HDBTHD.com】.DOC",
        "【更多无水印高清电影请访问 www.HDBTHD.com】【更多无水印高清电影请访问 www.HDBTHD.com】.MKV",
        # 变体: 换域名 / 换文案 / 换扩展名 / 裸域名带中文前缀
        "【更多无水印4K蓝光原盘请访问 www.BBEBBB.com】.mp4",
        "【首发于高清影视之家 www.BBQDDQ.com】.txt",
        "www.HDBTHD.com.url",
        "高清影视之家 www.hdbthd.com.mkv",
        "http://www.hdbthd.com/docs.html",
    ]
    MEDIA = [
        # 用户举例的正片(带广告前缀但必须保留)
        "【高清影视之家发布 www.HDBTHD.com】误杀2[60帧率版本][高码版][国语配音+中文字幕].Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD",
        "Fireflies.in.the.Sun.2021.2160p.HQ.WEB-DL.H265.60fps.DDP5.1.Atmos-DreamHD.mkv",
        "Glee.S06E01.Loser.Like.Me.1080p.DSNP.WEB-DL.DDP5.1.H.264-BlackTV.mkv",
        # CD2 上真实存在的目录名(不能被误判成广告)
        "欢乐合唱团.2009",
        "芝加哥烈焰.2012",
        "欢乐合唱团.Glee.S06.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV",
        "欢乐合唱团.S01.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV",
        "芝加哥烈焰.S03.2014.Amazon.WEB-DL.1080p.H264.DDP-Xiaomi",
        "谍网.Quantico.2015.S01.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV",
        "名侦探特辑",
        "头文字D",
        "奥特曼最全中文版（共22季）",
        "德云社郭德纲跨年北京站全程回顾.De.Yun.She.20200511.4K.WEB-DL.H265.AAC-MoeCatWEB",
        "绿箭侠 (2012)",
        "隋唐英雄3.Heroes.of.Sui.and.Tang.Dynasties.S03.2014.2160p.WEB-DL.H265.AAC-FLTTH",
        "大丈夫日记 (1964)",
        "指环王3：王者无敌 (2003)",
        "陌路狂杀",
        "先生贵姓 (1984)",
        "W武林世家 1984 张国荣 靖洋戏剧台",
        "X.现代爱情恋曲",
    ]

    ok = True
    print("=== 广告文件判定(应全部为 ad) ===")
    for n in ADS:
        c = classify_file(n, 289 * 1024)
        ok &= (c == "ad")
        print(f"  {'✓' if c == 'ad' else '✗'} {c:<9} {n[:80]}")

    print("\n=== 正片/目录判定(不应为 ad) ===")
    for n in MEDIA:
        c = classify_file(n)
        ok &= (c != "ad")
        print(f"  {'✓' if c != 'ad' else '✗'} {c:<9} {n[:80]}")

    print("\n=== 杂项/字幕/资产分类 ===")
    for n, want in [("readme.txt", "junk"), ("下载必看.doc", "junk"), ("更多资源.html", "junk"),
                    ("setup.exe", "junk"), ("NFO.nfo", "nfo"), ("movie.nfo", "nfo"),
                    ("poster.jpg", "asset"), ("fanart.png", "asset"),
                    ("误杀2.2021.tt16117346.chs.srt", "subtitle"),
                    ("误杀2.2021.2160p.mkv", "media")]:
        c = classify_file(n)
        ok &= (c == want)
        print(f"  {'✓' if c == want else '✗'} {c:<9} {n}  (期望 {want})")

    print("\n=== 标题/年份抽取 + 候选查询 ===")
    for n in [MEDIA[0], MEDIA[8], "谍网.Quantico.2015.S01.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV"]:
        t, y = extract_title_year(n)
        print(f"  {n[:72]}")
        print(f"    title={t!r} year={y!r}")
        print(f"    候选: {title_query_candidates(n, t, y)}")

    print("\n=== 目标目录名 ===")
    for args in [("误杀2", "2021", "tt16117346", 899665),
                 ("误杀2", "2021", "", 899665),
                 ("指环王3：王者无敌", "2003", "tt0167260", 122),
                 ("谍网", "2015", "", 62816),
                 ("欢乐合唱团", "2009", "tt1327801", 1417)]:
        print(f"  {args} -> {build_folder_name(*args)}")

    print("\n自测:", "全部通过 ✓" if ok else "❌ 有失败")

