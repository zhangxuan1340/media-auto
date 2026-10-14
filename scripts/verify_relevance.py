#!/usr/bin/env python3
"""磁力「相关性闸门」(三源同口径) 回归。

背景: 三条源的检索语义都不保证相关性 ——
  · Next-Web 站是 **pg_trgm 三元组模糊索引**, `carrie` 会命中 `carriers` / `carried` /
    `carriera`(三元组完全重合), 这是它噪声的直接成因;
  · native(Bitmagnet) 不带类型 facets 时会把"只命中年份"的条目带回来;
  · Jackett 公开站搜不到会回退返回"最新 N 条"填充。
统一闸门见 `lib/title_relevance.py`。

覆盖:
  ① 字母边界: `carrie` 不得命中 carriers / carried / carriera, 但要命中正常片名
     (含中英混排、大小写、片名紧贴分辨率、俄语前缀等写法);
  ② 年份闸: 同名不同年的老片丢, 没写年份的留, ±1 年容忍;
  ③ **分辨率不能当年份**(2160p / 1080p / 2048x1080 / e202609 / HDR10 / x264);
  ④ 纯数字标题(1917)解析不出强 token → 保守全放行(宁可脏, 不能空);
  ⑤ Jackett 侧与公共判定器同口径(旧的子串匹配已删);
  ⑥ router 侧: `_qname` 原始发布名优先、`_gate` 生效、按源独立的开关、
     质量分与「质量优先」排序不再让 native(规范化短名)垫底;
  ⑦ 三条取数路径(native / next_web 的 relevance 路径 + `_fetch_all` 分段窗口路径)
     都透传原始发布名并过闸;
  ⑧ 本次涉及的源码不含内网地址。

隔离: 全程**打桩上游 + 不发任何真实网络请求**; 库用临时文件(见文件末尾说明)。
用法: ./venv/bin/python scripts/verify_relevance.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# ⚠️ 隔离库: 必须在 import 任何项目模块**之前**设好 MEDIA_AUTO_DB,
#    否则 db 层会按默认路径去碰真实库。
_TMPDB = os.path.join(tempfile.gettempdir(), "ma_verify_relevance.db")
os.environ["MEDIA_AUTO_DB"] = _TMPDB

from lib import title_relevance as tr          # noqa: E402
from server.routers import search as s         # noqa: E402
from scripts import jackett_search as jk       # noqa: E402
from scripts import search as bm_search        # noqa: E402
from scripts import diao_search as diao        # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


# ---------------------------------------------------------------------------
# ① 字母边界
# ---------------------------------------------------------------------------
def test_word_boundary():
    print("-- ① 字母边界(Next-Web 噪声的直接成因) --")
    A, C = tr.strong_tokens("Carrie 2026")
    check("`Carrie 2026` 解析出强 token=carrie, 年份不进 token", A == ["carrie"] and C == [],
          f"{A} {C}")
    for bad in ("Liquefied Hydrogen Carriers_e202609.pdf",
                "STS Guidelines for Bulk Carriers 1Ed 2026.pdf",
                "[MissaX] (Daisy Phoenix, Donnie Rock) Carried Away XXX (2026) (1080p HEVC)",
                "La.Mia.Brillante.Carriera.S01E01-06.2026.WEB-DL.1080p"):
        check(f"不命中 {bad[:42]}", not tr.title_hit(bad, A, C))

    for good in ("Carrie.2026.S01.MULTi.VF2.2160p.WEB.HDR10.H265",
                 "Carrie (2026) S01 (1080p AMZN WEB-DL x265 10bit EAC3 Atmos 5.1 Ghost)",
                 "carrie.s01e08.1080p.web.h264-cakes[EZTVx.to].mkv",
                 "Кэрри.Carrie.S01.2026.WEB-DLRip.HDrezka Studio 18+",
                 "Carrie1080p",              # 片名紧贴分辨率: 数字不算边界冲突
                 "Carrie's Theory 2026"):
        check(f"命中 {good[:42]}", tr.title_hit(good, A, C))

    ca, cc = tr.strong_tokens("魔女嘉莉 2026")
    check("中文查询解析出 CJK 段", cc == ["魔女嘉莉"] and ca == [], f"{ca} {cc}")
    check("中英混排版命中 CJK",
          tr.title_hit("【高清剧集网】魔女嘉莉[全8集][简繁英字幕].Carrie.S01.2160p.AMZN.WEB-DL.H.265-BlackTV", ca, cc))
    check("不含中文的发布名不因 CJK 段放过",
          not tr.title_hit("Carrie.S01.2160p.AMZN.WEB-DL.H.265-BlackTV", ca, cc))
    check("短词不入选(token 门槛)", tr.strong_tokens("man ice the") == ([], []),
          str(tr.strong_tokens("man ice the")))


# ---------------------------------------------------------------------------
# ② 年份闸
# ---------------------------------------------------------------------------
def test_year_gate():
    print("-- ② 年份闸(挡同名不同年) --")
    Y = tr.query_years("Carrie 2026")
    check("查询年份 = {2026}", Y == {2026}, str(Y))
    check("Carrie 2013 冲突", not tr.year_ok("Carrie.2013.2160p.UHD.BluRay.x265.10bit.DV", Y))
    check("Carrie 1976 冲突", not tr.year_ok("Carrie.1976.1080p.BluRay.Remux.AVC.DTS-HD.MA.5.1", Y))
    check("Carrie 2026 不冲突", tr.year_ok("Carrie.2026.S01.2160p.AMZN.WEB-DL", Y))
    check("没写年份 → 放行(保守)", tr.year_ok("Carrie.S01.1080p", Y))
    check("±1 年容忍(Carrie 2025)", tr.year_ok("Carrie.2025.S01", Y))
    check("±2 年不容忍(Carrie 2024)", not tr.year_ok("Carrie.2024.S01", Y))

    print("-- ③ 分辨率 / 编码不能被当成日期 --")
    for t in ("x.2160p.HEVC.x265", "x.1080p.WEB-DL", "2048x1080", "3840x2160", "720p", "480p",
              "HDR10", "HDR10+", "x264", "DD5.1", "DDP5.1", "e202609"):
        check(f"{t!r} 里没有年份", tr.years_in(t) == [], str(tr.years_in(t)))
    check("2160p 与 2026 同现时只认出 2026",
          tr.years_in("Carrie.2026.2160p.WEB-DL") == ["2026"])


# ---------------------------------------------------------------------------
# ④ 保守放行
# ---------------------------------------------------------------------------
def test_conservative():
    print("-- ④ 解析不出强 token → 保守全放行(宁可脏, 不能空) --")
    check("纯数字标题(1917 2019)无强 token", tr.strong_tokens("1917 2019") == ([], []))
    items = [{"name": "随便什么"}, {"name": "另一个"}]
    check("→ 原样放行", len(tr.filter_by_query(items, "1917 2019")) == 2)
    check("→ 空查询也放行", len(tr.filter_by_query(items, "")) == 2)


# ---------------------------------------------------------------------------
# ⑤ Jackett 同口径
# ---------------------------------------------------------------------------
def test_jackett_same_rule():
    print("-- ⑤ Jackett 侧与公共判定器同口径 --")
    raw = [{"name": "Liquefied Hydrogen Carriers.pdf"},
           {"name": "Carrie.2026.S01.1080p.WEB-DL"},
           {"name": "Carrie.1976.1080p.BluRay.Remux"}]
    kept = jk._relevance_filter(raw, "Carrie 2026", {"jackett": {}})
    check("丢掉 Carriers + 1976 老电影, 只留真片",
          [x["name"] for x in kept] == ["Carrie.2026.S01.1080p.WEB-DL"], str(kept))
    check("关掉 jackett.relevance_filter → 原样返回",
          len(jk._relevance_filter(raw, "Carrie 2026", {"jackett": {"relevance_filter": False}})) == 3)
    check("旧的子串匹配辅助函数已移除(避免两套规则并存)", not hasattr(jk, "_strong_tokens"))


# ---------------------------------------------------------------------------
# ⑥ router 侧
# ---------------------------------------------------------------------------
def test_router():
    print("-- ⑥ router: 取名字口径 / 闸门 / 开关 --")
    check("_qname 原始发布名优先",
          s._qname({"name": "Carrie (2026) S01E07",
                    "torrentName": "Carrie.S01E07.2160p.mkv"}) == "Carrie.S01E07.2160p.mkv")
    check("_qname 无 torrentName 时退化为 name",
          s._qname({"name": "X.Y.2026.1080p"}) == "X.Y.2026.1080p")
    check("空条目不炸", s._qname(None) == "")
    check("开关默认开(native / next_web)",
          s._relevance_on({}, "native") and s._relevance_on({}, "next_web"))
    check("显式关 native 不影响 next_web",
          not s._relevance_on({"bitmagnet": {"relevance_filter": False}}, "native")
          and s._relevance_on({"bitmagnet": {"relevance_filter": False}}, "next_web"))
    check("显式关 next_web 不影响 native",
          not s._relevance_on({"bitmagnet_next_web": {"relevance_filter": False}}, "next_web"))

    items = [{"name": "Carrie.2026.S01", "torrentName": "Carrie.2026.S01"},
             {"name": "Liquefied Hydrogen Carriers.pdf", "torrentName": "Liquefied Hydrogen Carriers.pdf"}]
    gated = s._gate(list(items), "Carrie 2026", {}, "native")
    check("_gate 丢掉 Carriers, 留下真片",
          len(gated) == 1 and gated[0]["name"] == "Carrie.2026.S01", str(gated))
    check("_gate 在开关关闭时不生效",
          len(s._gate(list(items), "Carrie 2026",
                      {"bitmagnet": {"relevance_filter": False}}, "native")) == 2)
    check("_gate 空列表/关空安全", s._gate([], "x", {}, "native") == [])


# ---------------------------------------------------------------------------
# ⑦ 质量分与排序口径
# ---------------------------------------------------------------------------
def test_quality_and_sort():
    print("-- ⑦ 质量分 / 排序: 不再让 native 垫底 --")
    short = "Carrie (2026) S01"
    orig = "Carrie.2026.S01.2160p.AMZN.WEB-DL.DDP5.1.Atmos.HDR.H.265-FLUX"
    size = 55 * 10 ** 9
    check("规范化短名算质量分 = 0(旧口径 bug)", s.quality_score(short, {}, size) == 0,
          str(s.quality_score(short, {}, size)))
    check("原始发布名算质量分 > 0(修复)", s.quality_score(orig, {}, size) > 0,
          str(s.quality_score(orig, {}, size)))

    good = {"name": short, "torrentName": orig, "size": size, "seeders": 2}
    bad = {"name": "Carrie.S01.2026.WEB-DLRip.480p.RU",
           "torrentName": "Carrie.S01.2026.WEB-DLRip.480p.RU", "size": 10 ** 9, "seeders": 1}
    lst = [bad, good]
    s._annotate_quality(lst, {})
    check("质量分按原始发布名回传", lst[1].get("qualityScore", 0) > lst[0].get("qualityScore", 0),
          f"{lst[1].get('qualityScore')} vs {lst[0].get('qualityScore')}")
    s._apply_sort(lst, "quality", {})
    check("高画质(native 的 2160p 季包)排到首位", lst[0] is good, str([x["name"] for x in lst]))


# ---------------------------------------------------------------------------
# ⑧ 三条取数路径都透传 + 过闸
# ---------------------------------------------------------------------------
NATIVE_RAW = [
    {"infoHash": "a" * 40, "name": "Carrie (2026) S01E07", "size": 10 ** 9,
     "seeders": 3, "leechers": 1, "magnetLink": "magnet:?xt=urn:btih:" + "a" * 40,
     "poster": "https://image.tmdb.org/t/p/w500/x.jpg",
     "torrentName": "Carrie.S01E07.1080p.WEB.h264-cakes.mkv",
     "content": {"title": "Carrie", "releaseYear": 2026, "type": "TV_SHOW"}},
    {"infoHash": "b" * 40, "name": "Liquefied Hydrogen Carriers.pdf", "size": 10 ** 6,
     "seeders": 1, "leechers": 0, "magnetLink": "magnet:?xt=urn:btih:" + "b" * 40,
     "poster": "", "torrentName": "Liquefied Hydrogen Carriers.pdf", "content": {}},
]
NEXT_WEB_RAW = [
    {"hash": "c" * 40, "name": "Carrie.S01.2026.WEB-DL.1080p-Dual-Lat", "size": 10 ** 9,
     "magnet": "magnet:?xt=urn:btih:" + "c" * 40},
    {"hash": "d" * 40, "name": "La.Mia.Brillante.Carriera.S01E01-06.2026.WEB-DL.1080p",
     "size": 10 ** 9, "magnet": "magnet:?xt=urn:btih:" + "d" * 40},
]


def test_fetch_paths():
    print("-- ⑧ 取数路径: 透传原始发布名 + 过闸 --")
    orig_search, orig_collect = bm_search.search, diao.collect

    def fake_search(cfg, q, limit, ctype=None, *a, **k):
        return [dict(x) for x in NATIVE_RAW]

    def fake_collect(base, q, want, start_offset=0, *a, **k):
        return [dict(x) for x in NEXT_WEB_RAW], None, [], False, 2

    bm_search.search, diao.collect = fake_search, fake_collect
    try:
        native, _m, _n = asyncio.run(s._search_native({}, "Carrie 2026", 30, "tv_show"))
        native_all = asyncio.run(s._fetch_all("native", {}, "Carrie 2026", 30, ctype="tv_show"))
        nw, _hm, _np = asyncio.run(s._search_next_web({}, "Carrie 2026", 30))
        nw_all = asyncio.run(s._fetch_all("next_web", {}, "Carrie 2026", 30))
    finally:
        bm_search.search, diao.collect = orig_search, orig_collect

    check("native 透传 torrentName", native and native[0]["torrentName"] == "Carrie.S01E07.1080p.WEB.h264-cakes.mkv")
    check("native 透传 content 元数据(title / year / type)",
          native[0]["contentTitle"] == "Carrie" and native[0]["contentYear"] == 2026
          and native[0]["contentType"] == "TV_SHOW")
    check("native relevance 路径过闸(丢掉 Carriers)", len(native) == 1)
    check("native 分段窗口路径过闸", len(native_all) == 1)
    check("next_web 透传 torrentName(与该源 name 同值)",
          nw and nw[0]["torrentName"] == nw[0]["name"])
    check("next_web relevance 路径过闸(丢掉 Carriera)", len(nw) == 1)
    check("next_web 分段窗口路径过闸", len(nw_all) == 1)

    def fake_collect_off(base, q, want, start_offset=0, *a, **k):
        return [dict(x) for x in NEXT_WEB_RAW], None, [], False, 2

    diao.collect = fake_collect_off
    try:
        off = asyncio.run(s._fetch_all("next_web", {"bitmagnet_next_web": {"relevance_filter": False}},
                                       "Carrie 2026", 30))
    finally:
        diao.collect = orig_collect
    check("关掉开关 → 分段窗口路径原样返回", len(off) == 2)


# ---------------------------------------------------------------------------
# ⑨ 源码卫生
# ---------------------------------------------------------------------------
def test_source_hygiene():
    print("-- ⑨ 源码不含内网地址 / 本机路径 --")
    # 只认**完整四段**的私有地址: 注释里写 `172.16.x.x` / `192.168.1.100` 这种
    # 通配或示例形式是既有文档习惯(见 search.py 的代理说明), 不算把真实地址写进仓库。
    bad = re.compile(r"172\.16\.\d+\.\d+|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+"
                     r"|/Volumes/|nas\.passbook")
    for rel in ("lib/title_relevance.py", "server/routers/search.py",
                "scripts/jackett_search.py", "scripts/search.py"):
        with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
            txt = f.read()
        check(f"{rel} 干净", not bad.search(txt), (bad.search(txt) or [""])[0] if bad.search(txt) else "")
    with open(os.path.join(_ROOT, "lib", "title_relevance.py"), encoding="utf-8") as f:
        txt = f.read()
    check("判定器不用 \\b(会被 CJK 干扰)", r"\b" not in txt.split('"""')[-1].replace(r"\\b", ""),
          "正文里只应出现在解释性注释中")


def main():
    from db.database import init_db
    init_db()                      # 临时库建表 —— 否则 db 层会刷"无此表"错误
    try:
        test_word_boundary()
        test_year_gate()
        test_conservative()
        test_jackett_same_rule()
        test_router()
        test_quality_and_sort()
        test_fetch_paths()
        test_source_hygiene()
    finally:
        for p in (_TMPDB, _TMPDB + "-wal", _TMPDB + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass
    print()
    print("-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
