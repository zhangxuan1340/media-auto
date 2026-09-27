"""中文标题解析 —— 展示 / 目录名 / 文件名 / NFO <title> 全都读 tmdb_media.title

优先级(手动永远最高):
  1. tmdb_media.custom_title   详情页「中文标题」手动改的, 每次同步都会被 upsert 顶回
                               TMDB 值 → repositories.upsert_tmdb_media 写完后强制还原;
  2. TMDB 标题本身已含中文      tmdb.detail 已把 translations 里的中文译名并进 title;
  3. 豆瓣联想                   国内译名(实测 Bad Sisters → 坏姐妹), 结果缓存进
                               tmdb_media.title + title_checked, 每条最多查一次。

「含 CJK 即算中文」的判据里剔除了假名: 纯日文标题(含假名)不算中文, 仍会去豆瓣兜底。
台译「不良姐妹」也算中文 —— 想要「坏姐妹」请手动改一次, 改完 custom_title 永久生效。
"""
import re

from clients.douban import client as douban

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_SEASON_SUFFIX_RE = re.compile(
    r"(?:\s*第[0-9０-９一二三四五六七八九十百]+[季期部]|\s*Season\s*\d+|\s*S\d+)$", re.I)


def has_cn(text) -> bool:
    """是不是"中文标题"(含汉字且不含假名)。"""
    s = text or ""
    if not s or _KANA_RE.search(s):
        return False
    return bool(_CJK_RE.search(s))


def strip_season_suffix(title: str) -> str:
    """'坏姐妹 第一季' → '坏姐妹'(剧集目录名不该带季后缀)。"""
    s = (title or "").strip()
    prev = None
    while s and s != prev:
        prev = s
        s = _SEASON_SUFFIX_RE.sub("", s).strip()
    return s


def _norm(text: str) -> str:
    """比对用: 只留字母数字汉字, 全小写。"""
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", (text or "").lower())


def pick_cn_titles(translations) -> tuple:
    """从 TMDB 的 translations 响应挑中文标题, 返回 (大陆标题, 其余中文兜底)。

    为什么要分两路: 大陆那条的 name 经常是**空串**(TMDB 只翻了剧情没翻标题,
    实测 Bad Sisters tmdb 199318: zh-CN 为空、台译「不良姐妹」、港译「非常姊妹大作戰」)。
    于是:
      - 大陆有 → 直接用(最准的国内译名);
      - 大陆空 → 返回台/港等兜底, 但**先让豆瓣试**(国内译名), 豆瓣查不到再用兜底。
    任一没有则返回空串。
    """
    if not isinstance(translations, dict):
        return "", ""
    trs = translations.get("translations") or []
    if not isinstance(trs, list):
        return "", ""
    exact, region, loose = {}, {}, []
    for t in trs:
        if not isinstance(t, dict):
            continue
        iso = (t.get("iso_639_1") or "").lower()
        name = ((t.get("data") or {}).get("name") or "").strip()
        if not name or not iso.startswith("zh"):
            continue
        if "-" in iso:
            exact[iso.split("-", 1)[1]] = name
        else:
            region[(t.get("iso_3166_1") or "").upper()] = name
            loose.append(name)
    mainland = exact.get("cn") or region.get("CN") or ""
    other = ""
    for key in ("tw", "hk", "mo", "sg"):
        val = exact.get(key) or region.get(key.upper()) or ""
        if val and val != mainland:
            other = val
            break
    if not other:
        other = next((n for n in loose if n and n != mainland), "")
    return mainland, other


def pick_cn_title(translations) -> str:
    """(大陆, 其余中文) 里第一个非空的 —— 单一标题场景用这个。"""
    mainland, other = pick_cn_titles(translations)
    return mainland or other


def douban_cn_title(query: str, year="", kind="") -> tuple:
    """豆瓣联想 → 国内译名。返回 (title, status), status ∈ {hit, none, error}。

    匹配打分: sub_title 与原名同源 +3 / 年份 ±1 +2 / 剧集(带 episode)+1;
    总分 < 3 视为没把握 → none(宁可不改, 也不把标题改错)。
    """
    if not (query or "").strip():
        return "", "none"
    try:
        items = douban.suggest(query)
    except Exception:  # noqa: BLE001  网络/反爬失败 → error, 下次重试
        return "", "error"
    if not items:
        return "", "none"
    q = _norm(query)
    try:
        y = int(str(year or "0")[:4] or 0)
    except ValueError:
        y = 0
    best, best_score = None, 0
    for it in items:
        if not isinstance(it, dict):
            continue
        score = 0
        sub = _norm(it.get("sub_title") or "")
        if q and sub and (sub == q or q in sub or sub in q):
            score += 3
        iy = str(it.get("year") or "")[:4]
        if y and iy.isdigit() and abs(int(iy) - y) <= 1:
            score += 2
        if kind == "tv" and it.get("episode"):
            score += 1
        if score > best_score:
            best, best_score = it, score
    if not best or best_score < 3:
        return "", "none"
    title = (best.get("title") or "").strip()
    if kind == "tv":
        title = strip_season_suffix(title)
    if not has_cn(title):
        return "", "none"
    return title, "hit"


def apply_to_row(cfg, kind: str, row: dict, existing=None, fallback: str = "") -> None:
    """同步写库**之前**给 row['title'] 做中文兜底(就地改, 由 upsert 落库)。

    existing = 库里旧行(读 custom_title / title_checked);fallback = TMDB 的台/港译名
    (大陆为空时的退路, 见 pick_cn_titles)。
    豆瓣网络失败时不置 title_checked → 下次同步重试, 不把"暂时查不到"固化成"永远没有"。
    """
    custom = (getattr(existing, "custom_title", "") or "").strip() if existing else ""
    if custom:
        row["title"] = custom
        row["title_checked"] = True
        return
    if has_cn(row.get("title")):
        row["title_checked"] = True
        return
    if existing is not None and getattr(existing, "title_checked", False):
        # 已核对过 → 不再查豆瓣; 但必须**沿用上次解析出的中文标题**:
        # 同步每轮都用 TMDB 的 title 覆盖 row, 不还原的话库里中文会被打回英文
        # (实测 Bad Sisters: 库里 坏姐妹 → 一次同步后变回 Bad Sisters)。
        if not has_cn(row.get("title")):
            prev = (getattr(existing, "title", "") or "").strip()
            if prev and has_cn(prev):
                row["title"] = prev
            elif fallback:
                # 库里也没中文(上次落成英文) → 台/港兜底照样套:
                # "豆瓣查过"与"用不用兜底"是两件事, 少了这步英文状态永不自愈。
                row["title"] = fallback
        return
    zh, status = douban_cn_title(row.get("original_title") or row.get("title") or "",
                                 row.get("year") or "", kind)
    if zh:
        row["title"] = zh
    elif fallback:
        row["title"] = fallback          # 豆瓣没有 → 用 TMDB 台/港译名, 至少不是英文
    if status in ("hit", "none"):
        row["title_checked"] = True


def apply_to_meta(cfg, kind: str, tmdb_id, meta: dict, *, read_only: bool = False) -> dict:
    """直连 TMDB 拿到的 meta(title/originalTitle/...)做同样的兜底, 并回写本地行。

    用在 NFO 重建(元数据以 TMDB 直连为准, 会绕过本地行的中文标题)。
    meta["zh_fallback"] 是 detail() 带回的 TMDB 台/港译名(大陆为空时的退路)。

    read_only=True: **只算不写** —— 干跑(rename?dry_run)也要看到最终标题, 但不能
    顺手把 title/title_checked 钉进库、也不能消耗豆瓣"每条一次"的核对标记。
    """
    from db.database import SessionLocal       # noqa: PLC0415  避免模块级环
    from db import repositories as repo        # noqa: PLC0415

    fallback = (meta.get("zh_fallback") or "").strip()
    s = SessionLocal()
    try:
        obj = repo.get_tmdb_media_by_id(s, kind, tmdb_id)
        custom = (getattr(obj, "custom_title", "") or "").strip() if obj else ""
        if custom:
            meta["title"] = custom
            return meta
        if has_cn(meta.get("title")):
            if not read_only and obj is not None and not obj.title_checked:
                obj.title_checked = True
                s.commit()
            return meta
        # 直连拿到的是英文, 但库里已经解析出中文(同步/上次豆瓣) → 以库里为准。
        # 漏掉这一步的话「更新 NFO / 按新标题重命名」会拿英文标题把中文目录改回去。
        if obj is not None and has_cn(obj.title):
            meta["title"] = obj.title
            return meta
        if obj is not None and obj.title_checked:
            # 已核对过(豆瓣每条最多查一次) → 不再查, 但**台/港兜底照样要套**:
            # 不套的话这次会把 TMDB 英文标题直接写进 NFO/改名, 与上一次写的中文
            # 互相跳变(实测: 第 1 次「更新 NFO」写中文, 第 2 次变回英文)。
            if not has_cn(meta.get("title")) and fallback:
                meta["title"] = fallback
                if not read_only and obj and not has_cn(obj.title or ""):
                    obj.title = fallback   # 回写库, 否则库里永远停在英文
                    s.commit()
            return meta
        # 豆瓣查询词用【原名】: meta 里是 originalTitle(TMDB detail 的键名),
        # 早期写成不存在的 original_title → 实际退回显示名, 与同步路径查询词不一致。
        zh, status = douban_cn_title(meta.get("originalTitle") or meta.get("original_title")
                                     or meta.get("title") or "",
                                     meta.get("year") or "", kind)
        if zh:
            meta["title"] = zh
            if not read_only and obj is not None:
                obj.title = zh
        elif fallback:
            meta["title"] = fallback
            if not read_only and obj is not None:
                # 台/港兜底也回写库: 只写 meta 的话, 下次同步用 TMDB 英文覆盖
                # row 后再走到这里, 库内标题永远是英文(与 apply_to_row 口径不一致)。
                obj.title = fallback
        if not read_only and obj is not None:
            if status in ("hit", "none"):
                obj.title_checked = True
            s.commit()
        return meta
    finally:
        s.close()
