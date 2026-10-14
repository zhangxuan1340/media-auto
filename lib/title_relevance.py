#!/usr/bin/env python3
"""种子标题的「相关性判定」—— 三条磁力源共用的同一道闸门。

## 为什么必须自己判

三条源的检索语义完全不同, 但没有一条能保证「返回的就跟查询有关」:

  · **next_web**(改版站) 站底子是 **pg_trgm 三元组模糊索引**(见 scripts/diao_search.py
    的 docstring)。`carrie` 被切成 `car/arr/rri/rie` 四个三元组, 而 `carriers` /
    `carried` / `carriera` 的三元组与它**完全重合** —— 在索引眼里就是同一个词。
    于是「氢载体 PDF」「Carried Away 成人片」「La Mia Brillante Carriera 意大利电影」
    全被当成高相似度返回。它还会把查询拆词做 OR 匹配, 于是只命中 `2026` 的条目也进来。
  · **native**(Bitmagnet) 是正经全文检索 + TMDB 元数据匹配, 带上类型 facets 时很准;
    但**不带 ctype**、或类型本身是影视之外时, 同样会把「只命中年份」的条目带回来。
  · **Jackett** 的公开站在**搜不到**时会回退返回「最新 N 条」填充(与查询完全无关)。

所以过滤不能指望上游, 必须自己过一道: **标题命中 + 年份不冲突**。

## 判定规则

① **标题命中** —— 查询词里的「强 token」至少有一个出现在标题里。
   ⚠️ 拉丁词必须按**字母边界**匹配, 绝不能子串: `carrie` 用子串会命中 `carriers`,
      这正是 next_web 噪声的根因。
   ⚠️ 不用 `\\b`: Python 的 `\\w` 把 CJK 也算单词字符, `\\bcarrie\\b` 在「魔女嘉莉carrie」
      这种中英紧贴处会判错边界。改用 `(?<![a-z])…(?![a-z])` —— 只约束拉丁字母,
      **不约束数字**(`Carrie1080p` 这种片名+分辨率的写法仍要算命中)。
   ⚠️ CJK 没有空格分词, 只能按**连续段子串**匹配(标题里出现 `魔女嘉莉` 即命中)。

② **年份不冲突** —— 标题里若写了年份, 至少要有一个落在目标年 ±1 内。
   挡的是「同名不同年」: 搜 2026 的《魔女嘉莉》剧集时, `Carrie 2013` / `Carrie 1976`
   这两部电影必须丢掉。
   ⚠️ 标题里**一个年份都没有 → 放行**。发布名常常不写年份, 保守不能丢。
   ⚠️ 年份正则只认 `19xx` / `20[0-3]x`: 既排除 `2160p` / `1080p` / `2048x1080` 这类
      分辨率(它们本身不是年份), 也靠 `(?!\\d)` 避免把 `e202609` 里的 `2026` 当成词。

## 保守原则

**查询解析不出任何强 token(极短词/纯数字标题/纯符号) → 全部放行。**
宁可脏, 也不能因为判定器不理解就把结果清空。同理, 年份解析不出来时不做年份约束。
"""

import re

# 拉丁 token: 字母开头, 可含数字与撇号(Don't / 1917 这类纯数字会被下面的规则剔掉)
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']*")
# CJK 连续段(基本区 + 扩展 A), 与 scripts/jackett_search.py 的历史口径一致
_CJK_TOKEN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
# 年份: 只认 1900-2039。2160p / 1080p / 2048x1080 / 3840x2160 全部落在窗口外
_YEAR_RE = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-3][0-9])(?!\d)")

# 年份容忍度: 首播年 / 制作年 / 发布年可能差一年, 差 1 年不作冲突处理
_YEAR_SLACK = 1
# 拉丁强 token 的最短长度: 短于此的词(man / ice / the)区分度太低, 进了会误伤
_MIN_ASCII = 4
_MIN_CJK = 2


def strong_tokens(query):
    """查询词里的「强 token」→ (拉丁词表, CJK 段表), 均已去重、保序。

    拉丁词要 ≥4 字符且不是纯数字(年份单独走年份闸, 不该当标题 token)。
    """
    ascii_toks, seen_a = [], set()
    for w in _ASCII_TOKEN_RE.findall(query or ""):
        wl = w.lower()
        if len(wl) >= _MIN_ASCII and not wl.isdigit() and wl not in seen_a:
            seen_a.add(wl)
            ascii_toks.append(wl)
    cjk_toks, seen_c = [], set()
    for w in _CJK_TOKEN_RE.findall(query or ""):
        if len(w) >= _MIN_CJK and w not in seen_c:
            seen_c.add(w)
            cjk_toks.append(w)
    return ascii_toks, cjk_toks


def years_in(text):
    """文本里出现的年份(1900-2039), 去重保序。

    `2160p` / `1080p` / `2048x1080` 不会被误认 —— 见 `_YEAR_RE` 的窗口与边界。
    `e202609` 里的 `2026` 也不会(后面跟着数字, 被 `(?!\\d)` 挡掉)。
    """
    out, seen = [], set()
    for y in _YEAR_RE.findall(text or ""):
        if y not in seen:
            seen.add(y)
            out.append(y)
    return out


def query_years(query):
    """查询串里的年份集合(整数)。前端把「标题 + 年份 + 季标记」拼成一个 q,
    所以这里的年份就是目标年份; 标题自带年份时会在同一个集合里, 一并算作允许值。"""
    return {int(y) for y in years_in(query)}


def title_hit(name, ascii_toks, cjk_toks):
    """标题是否命中任一强 token。"""
    if not name:
        return False
    low = name.lower()
    for t in ascii_toks or ():
        # 字母边界: 前后紧邻的不是英文字母。数字不算边界冲突(Carrie1080p 要算命中)
        if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low):
            return True
    return any(t in name for t in cjk_toks or ())


def year_ok(name, years):
    """标题里的年份是否与目标年不冲突。

    - 标题没写年份 → True(保守放行)
    - 没有任何目标年份 → True(不做约束)
    - 否则: 标题里至少有一个年份落在某个目标年的 ±_YEAR_SLACK 内
    """
    ys = years_in(name)
    if not ys:
        return True
    allowed = {int(y) for y in (years or ()) if y}
    if not allowed:
        return True
    return any(abs(int(v) - a) <= _YEAR_SLACK for v in ys for a in allowed)


def passes(name, ascii_toks, cjk_toks, years=None):
    """单条判定: 标题命中 **且** 年份不冲突。"""
    if not ascii_toks and not cjk_toks:
        return True                      # 解析不出强 token → 放行
    if not title_hit(name, ascii_toks, cjk_toks):
        return False
    return year_ok(name, years or ())


def filter_by_query(items, query, name_of=None, years=None):
    """按查询串过滤一批条目, 返回**新列表**(保序)。

    name_of: 取「用于判定的标题」的函数, 默认 `item['name']`。
      ⚠️ 调用方通常要传「原始发布名优先」的取值器 —— Bitmagnet 匹配到 TMDB 后
         会把 `name` 换成规范化短名(`Carrie (2026) S01E07`), 那里既没有年份也没有
         画质标记, 拿它做年份判定会一律「无年份 → 放行」, 闸门就失效了。
    years: 目标年份集合(整数); 不传则从 query 里抽。
    """
    name_of = name_of or (lambda it: (it or {}).get("name") or "")
    ascii_toks, cjk_toks = strong_tokens(query)
    if not ascii_toks and not cjk_toks:
        return list(items)               # 保守: 判定不了就不动
    yrs = set(years) if years else query_years(query)
    out = []
    for it in items:
        nm = name_of(it) or ""
        if title_hit(nm, ascii_toks, cjk_toks) and year_ok(nm, yrs):
            out.append(it)
    return out
