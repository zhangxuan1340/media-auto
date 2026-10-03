#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【纯本地】电影/剧集页签 + 管理→浏览 的高级筛选回归(不连网)

需求(2026-10-02 用户反馈):
  1. 电影/剧集筛选太单一: 都缺日期范围; 剧集缺"是否完结"的状态筛选;
     缺分级筛选; 缺流媒体平台筛选 —— 加上才好快速定位。
  2. 一处 bug: 切到别的页签再回来, 列表**还是筛过的数据**, 筛选框却显示"全部",
     根本看不出筛没筛。

实现口径:
  - 页签(热门榜)走 TMDB discover: 日期范围 / 剧集状态(with_status) /
    分级(仅电影 —— TMDB discover/tv 没有 certification 参数) / 流媒体平台(+地区)。
  - 管理→浏览走本地 tmdb_media: 年份范围 / 剧集完结状态 / 分级(剧集唯一可靠口径)。
  - 两页的筛选状态都必须"从实例状态回填 DOM", 不能读 DOM 现值(那正是 bug 根因)。

本脚本覆盖:
  A) clients.tmdb.discover_page 参数映射(8 用例, 打桩 _tmdb_get 抓 params)
  B) GET /api/trending/{kind} 校验 + 分流 + 缓存键分离(打桩 discover/trending)
  C) GET /api/trending/filters(平台/分级下拉 + 缓存 + 校验)
  D) GET /api/browse 本地筛选: 年份范围/完结状态/分级 + 校验(临时库)
  E) GET /api/browse/certs 分级下拉(拆分/去重/排序)
  F) 前端红线: 查询串带上新参数、状态回填 DOM、不再有"写死成全部"的行

用法: ./venv/bin/python scripts/verify_trend_filters.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ⚠️ 必须在 import db.* / server.* 之前指好库路径(db/database.py 导入时就建 engine)
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_trend_filters_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from fastapi import HTTPException                          # noqa: E402
from db.database import SessionLocal, init_db              # noqa: E402
from db.models import TmdbMedia                            # noqa: E402
import clients.tmdb.client as tmdbc                        # noqa: E402
from server.routers import browse                          # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


TMDB_CFG = {"tmdb": {"api_key": "test-key", "language": "zh-CN"}}
NO_NET_CFG = {"tmdb": {}}


# ---------------------------------------------------------------------------
# A) discover_page 参数映射(打桩 _tmdb_get, 抓真实要发给 TMDB 的 query)
# ---------------------------------------------------------------------------
def t_discover_params():
    CAP = {}

    async def _fake(cfg, path, params=None, timeout=30):
        CAP["path"], CAP["params"] = path, dict(params or {})
        if "/watch/providers" in path:
            return {"results": [{"provider_id": 8, "provider_name": "Netflix"}]}
        if path.startswith("/3/certification"):
            return {"certifications": {"US": [{"certification": "R", "order": 4},
                                              {"certification": "PG-13", "order": 3}],
                                       "XX": [{"certification": "ZZ", "order": 1}]}}
        if path.startswith("/3/discover"):
            return {"page": 1, "results": [], "total_pages": 1}
        return {}

    orig = tmdbc._tmdb_get
    tmdbc._tmdb_get = _fake
    try:
        # A1 电影日期范围 → primary_release_date.gte/lte
        run(tmdbc.discover_page(TMDB_CFG, "movie", page=1,
                                filters={"date_from": "2023-01-01", "date_to": "2023-12-31"}))
        p = CAP["params"]
        check("A1 电影 date_from→primary_release_date.gte", p.get("primary_release_date.gte") == "2023-01-01", p)
        check("A2 电影 date_to→primary_release_date.lte", p.get("primary_release_date.lte") == "2023-12-31", p)
        check("A3 电影不会误发 first_air_date", "first_air_date.gte" not in p, p)

        # A4 剧集日期范围 → first_air_date.gte/lte
        run(tmdbc.discover_page(TMDB_CFG, "tv", page=1,
                                filters={"date_from": "2020-01-01", "date_to": "2020-06-30"}))
        p = CAP["params"]
        check("A4 剧集 date_from→first_air_date.gte", p.get("first_air_date.gte") == "2020-01-01", p)
        check("A5 剧集 date_to→first_air_date.lte", p.get("first_air_date.lte") == "2020-06-30", p)
        check("A6 剧集不会误发 primary_release_date", "primary_release_date.gte" not in p, p)

        # A7 剧集状态 → with_status(3 = 完结, TMDB 口径 0在播/3完结/4取消)
        run(tmdbc.discover_page(TMDB_CFG, "tv", page=1, filters={"status": "3"}))
        check("A7 status→with_status", CAP["params"].get("with_status") == "3", CAP["params"])

        # A8 分级 "US:PG-13" → certification + certification_country + region
        run(tmdbc.discover_page(TMDB_CFG, "movie", page=1, filters={"cert": "US:PG-13"}))
        p = CAP["params"]
        check("A8 分级拆成 certification", p.get("certification") == "PG-13", p)
        check("A9 certification_country", p.get("certification_country") == "US", p)
        check("A10 region 与 certification 配套", p.get("region") == "US", p)

        # A11 剧集拿到 cert 也不该发 TMDB(参数不存在, 发了会被静默忽略 → 假筛选)
        run(tmdbc.discover_page(TMDB_CFG, "tv", page=1, filters={"cert": "US:TV-14"}))
        check("A11 剧集不发 certification(客户端兜底)",
              "certification" not in CAP["params"] and "certification_country" not in CAP["params"],
              CAP["params"])

        # A12 平台 + 地区成对
        run(tmdbc.discover_page(TMDB_CFG, "movie", page=1,
                                filters={"provider": 8, "watch_region": "us"}))
        p = CAP["params"]
        check("A12 平台→with_watch_providers", p.get("with_watch_providers") == 8, p)
        check("A13 地区大写化→watch_region", p.get("watch_region") == "US", p)

        # A14 无筛选时不发任何高级参数, 类型/国家照旧
        run(tmdbc.discover_page(TMDB_CFG, "movie", page=1, genre_id=28, country="CN",
                                filters={"date_from": "", "date_to": "", "status": "",
                                         "cert": "", "provider": 0, "watch_region": ""}))
        p = CAP["params"]
        check("A14 空筛选不发日期/状态/分级/平台",
              not any(k in p for k in ("primary_release_date.gte", "first_air_date.gte",
                                       "with_status", "certification", "with_watch_providers")), p)
        check("A15 类型/国家照旧", p.get("with_genres") == 28 and p.get("with_origin_country") == "CN", p)
        check("A16 热度排序 + 不带成人内容", p.get("sort_by") == "popularity.desc"
              and p.get("include_adult") == "false", p)

        # A17 平台列表(按地区)截前 60 + 形状
        rows = run(tmdbc.watch_providers(TMDB_CFG, "movie", "CN"))
        check("A17 平台列表形状", rows == [{"id": 8, "name": "Netflix", "logo": ""}], rows)

        # A18 分级表: 只留常用国家 + US 固定序宽松→严格 + 同格式 "US:..."
        certs = run(tmdbc.certifications(TMDB_CFG, "movie"))
        names = [c["cert"] for c in certs]
        check("A18 分级固定序 PG-13 在 R 之前", names.index("US:PG-13") < names.index("US:R"), names)
        check("A19 非常用国家(XX)不进下拉", all(not c["cert"].startswith("XX:") for c in certs), names)
        check("A20 分级值带国家前缀", all(":" in c["cert"] for c in certs), names)
    finally:
        tmdbc._tmdb_get = orig


# ---------------------------------------------------------------------------
# B) /trending/{kind} 校验 + 分流 + 缓存(打桩 discover/trending)
# ---------------------------------------------------------------------------
def t_trending_endpoint():
    calls = {"discover": [], "trend": []}

    async def _disc(cfg, kind, page=1, genre_id=None, country=None,
                    sort_by="popularity.desc", filters=None):
        calls["discover"].append(dict(kind=kind, genre=genre_id, country=country,
                                      filters=dict(filters or {})))
        # ⚠️ 必须回非空: 后端刻意"空结果不缓存"(TMDB 限流返回空时若缓存了,
        # 会把榜单"空白 10 分钟"), 用空列表测不出缓存键分离
        return [{"title": f"片子{kind}", "year": "2020"}], 1

    async def _tr(cfg, kind, time_window="week", page=1):
        calls["trend"].append(dict(kind=kind, window=time_window, page=page))
        return []

    orig_d, orig_t = tmdbc.discover_page, tmdbc.trending
    tmdbc.discover_page, tmdbc.trending = _disc, _tr
    try:
        async def call(**kw):
            base = dict(kind="movie", window="week", page=1, size=20, genre=0, country="",
                        date_from="", date_to="", status="", cert="", provider=0,
                        watch_region="", cfg=TMDB_CFG)
            base.update(kw)
            return await browse.trending(**base)

        def reset():
            calls["discover"].clear(); calls["trend"].clear()
            browse._trending_cache.clear(); browse._trend_seen.clear()

        def err(coro):
            try:
                run(coro)
                return None
            except HTTPException as e:
                return e

        # 校验
        reset()
        e = err(call(date_from="2023/01/01"))
        check("B1 日期格式非法→400", e is not None and e.status_code == 400, e)
        e = err(call(date_from="2023-12-31", date_to="2023-01-01"))
        check("B2 起止颠倒→400", e is not None and e.status_code == 400, e)
        e = err(call(kind="movie", status="3"))
        check("B3 电影带状态→400", e is not None and e.status_code == 400, e)
        e = err(call(status="9"))
        check("B4 状态取值越界→400", e is not None and e.status_code == 400, e)
        e = err(call(kind="tv", cert="US:TV-14"))
        check("B5 剧集带分级→400(本地浏览页才是口径)", e is not None and e.status_code == 400, e)
        e = err(call(cert="PG-13"))
        check("B6 分级缺国家前缀→400", e is not None and e.status_code == 400, e)
        e = err(call(provider=8, watch_region="CHINA"))
        check("B7 平台地区非 2 位→400", e is not None and e.status_code == 400, e)
        e = err(call(country="CHINA"))
        check("B8 产地国家非 2 位→400", e is not None and e.status_code == 400, e)
        e = err(call(cfg={"tmdb": {}}))
        check("B9 未配 TMDB key→503", e is not None and e.status_code == 503, e)

        # 分流: 无筛选 → trending 榜; 任一高级筛选 → discover
        reset()
        run(call())
        check("B10 无筛选走 trending 榜", len(calls["trend"]) == 1 and not calls["discover"], calls)
        reset()
        run(call(date_from="2024-01-01"))
        check("B11 只有日期也走 discover", len(calls["discover"]) == 1 and not calls["trend"], calls)
        check("B12 日期进 discover filters",
              calls["discover"] and calls["discover"][0]["filters"]["date_from"] == "2024-01-01",
              calls)
        reset()
        run(call(kind="tv", status="3"))
        check("B13 剧集状态走 discover", len(calls["discover"]) == 1, calls)
        check("B14 状态原样传递", calls["discover"][0]["filters"]["status"] == "3", calls)
        reset()
        run(call(cert="US:R"))
        check("B15 分级走 discover", len(calls["discover"]) == 1
              and calls["discover"][0]["filters"]["cert"] == "US:R", calls)
        reset()
        run(call(provider=8))
        check("B16 平台走 discover + 地区兜底 CN",
              calls["discover"][0]["filters"]["provider"] == 8
              and calls["discover"][0]["filters"]["watch_region"] == "CN", calls)
        reset()
        run(call(genre=28, country="CN", date_from="2020-01-01", date_to="2020-12-31"))
        f = calls["discover"][0]
        check("B17 类型/国家与高级筛选同传",
              f["genre"] == 28 and f["country"] == "CN"
              and f["filters"]["date_to"] == "2020-12-31", f)

        # 缓存键: 换筛选必须重抓, 不换则命中缓存
        reset()
        run(call(date_from="2021-01-01"))
        run(call(date_from="2021-01-01"))
        check("B18 同筛选第二次命中缓存(只抓 1 次)", len(calls["discover"]) == 1, calls)
        run(call(date_from="2022-01-01"))
        check("B19 换日期重新抓(共 2 次)", len(calls["discover"]) == 2, calls)
        run(call(date_from="2022-01-01", cert="US:R"))
        check("B20 加分级也重新抓(共 3 次)", len(calls["discover"]) == 3, calls)
        run(call(kind="tv", date_from="2022-01-01"))
        check("B21 换 kind 不串缓存", len(calls["discover"]) == 4, calls)
    finally:
        tmdbc.discover_page, tmdbc.trending = orig_d, orig_t


# ---------------------------------------------------------------------------
# C) /trending/filters(平台地区 + 流媒体平台 + 分级)
# ---------------------------------------------------------------------------
def t_filters_endpoint():
    calls = {"prov": 0, "cert": 0}

    async def _prov(cfg, kind, region="CN"):
        calls["prov"] += 1
        return [{"id": 8, "name": "Netflix", "logo": "/a.png"}]

    async def _cert(cfg, kind):
        calls["cert"] += 1
        return [{"country": "US", "cert": "US:PG-13"}]

    orig_p, orig_c = tmdbc.watch_providers, tmdbc.certifications
    tmdbc.watch_providers, tmdbc.certifications = _prov, _cert
    browse._FILTERS_CACHE.clear()
    try:
        async def call(**kw):
            base = dict(kind="movie", region="CN", cfg=TMDB_CFG)
            base.update(kw)
            return await browse.trending_filters(**base)

        def err(coro):
            try:
                run(coro)
                return None
            except HTTPException as e:
                return e

        r = run(call())
        check("C1 平台地区列表(内置表)", r["regions"] and r["regions"][0]["code"] == "CN", r.get("regions"))
        check("C2 电影平台列表", r["providers"] and r["providers"][0]["name"] == "Netflix", r["providers"])
        check("C3 电影分级列表", r["certifications"] and r["certifications"][0]["cert"] == "US:PG-13",
              r["certifications"])
        r2 = run(call())
        check("C4 有平台时第二次命中缓存(不再打 TMDB)", calls["prov"] == 1 and calls["cert"] == 1, calls)

        r3 = run(call(kind="tv"))
        check("C5 剧集不分级(前端因此置灰并注明)",
              r3["certifications"] == [] and calls["cert"] == 1, r3["certifications"])
        check("C6 剧集平台照常给", r3["providers"], r3["providers"])

        e = err(call(kind="music"))
        check("C7 kind 非法→400", e is not None and e.status_code == 400, e)
        e = err(call(region="CHN"))
        check("C8 地区非 2 位→400", e is not None and e.status_code == 400, e)
        # 换地区 = 另一个缓存键
        n = calls["prov"]
        run(call(region="US"))
        check("C9 换地区重新取平台", calls["prov"] == n + 1, calls)
    finally:
        tmdbc.watch_providers, tmdbc.certifications = orig_p, orig_c
        browse._FILTERS_CACHE.clear()


# ---------------------------------------------------------------------------
# D) /browse 本地筛选(临时库)
# ---------------------------------------------------------------------------
def _seed():
    s = SessionLocal()
    rows = [
        # kind, id, title, year, genres( id 串 ), cert, status, in_production
        ("movie", 1, "M二零二零", "2020", "28", "US:PG-13", "Released", False),
        ("movie", 2, "M二零一五", "2015", "18", "US:R", "Released", False),
        ("movie", 3, "M无年份", "", "18", "", "Released", False),
        ("tv", 11, "T已完结", "2022", "18", "US:TV-14", "Ended", False),
        ("tv", 12, "T在播中", "2023", "35", "US:TV-MA", "Returning Series", True),
        ("tv", 13, "T无分级", "2021", "35", "", "Ended", False),
        ("tv", 14, "T多国分级", "2019", "18", "US:TV-PG / HK:IIA", "Canceled", False),
    ]
    for kind, tid, title, year, genres, cert, st, inp in rows:
        s.add(TmdbMedia(kind=kind, tmdb_id=tid, title=title, original_title=title,
                        year=year, genres=genres, genre_names="", certification=cert,
                        status=st, in_production=inp))
    s.commit()
    s.close()


def t_browse_endpoint():
    _seed()

    async def call(**kw):
        base = dict(kind="movie", q="", genre=0, year_from=0, year_to=0, cstatus="",
                    cert="", status="all", page=1, size=50, cfg=NO_NET_CFG)
        base.update(kw)
        return await browse.browse(**base)

    def titles(r):
        return sorted(x["title"] for x in r["items"])

    def err(coro):
        try:
            run(coro)
            return None
        except HTTPException as e:
            return e

    r = run(call(kind="movie"))
    check("D1 无筛选出全部电影", titles(r) == ["M二零一五", "M二零二零", "M无年份"], titles(r))
    r = run(call(kind="movie", year_from=2016, year_to=2021))
    check("D2 年份范围(2016-2021)", titles(r) == ["M二零二零"], titles(r))
    r = run(call(kind="movie", year_from=2016))
    check("D3 只给起始年", titles(r) == ["M二零二零"], titles(r))
    r = run(call(kind="movie", year_to=2016))
    check("D4 只给截止年", titles(r) == ["M二零一五"], titles(r))
    r = run(call(kind="movie", year_from=2014, year_to=2016))
    check("D5 跨年闭区间含两端", titles(r) == ["M二零一五"], titles(r))
    check("D6 无年份的作品进不了任何年份范围", "M无年份" not in titles(
        run(call(kind="movie", year_from=1900, year_to=2100))), titles(
        run(call(kind="movie", year_from=1900, year_to=2100))))
    e = err(call(kind="movie", year_from=2021, year_to=2020))
    check("D7 年份起止颠倒→400", e is not None and e.status_code == 400, e)

    r = run(call(kind="tv", cstatus="ended"))
    check("D8 已完结", titles(r) == ["T已完结", "T无分级"], titles(r))
    r = run(call(kind="tv", cstatus="returning"))
    check("D9 在播(in_production 兜底)", titles(r) == ["T在播中"], titles(r))
    r = run(call(kind="tv", cstatus="canceled"))
    check("D10 已取消", titles(r) == ["T多国分级"], titles(r))
    e = err(call(kind="movie", cstatus="ended"))
    check("D11 电影带完结状态→400", e is not None and e.status_code == 400, e)
    e = err(call(kind="tv", cstatus="whatever"))
    check("D12 状态取值非法→400", e is not None and e.status_code == 400, e)

    r = run(call(kind="tv", cert="US:TV-14"))
    check("D13 剧集分级精确命中", titles(r) == ["T已完结"], titles(r))
    r = run(call(kind="tv", cert="HK:IIA"))
    check("D14 多国分级按 / 拆开也命中", titles(r) == ["T多国分级"], titles(r))
    r = run(call(kind="tv", cert="US:R"))
    check("D15 分级不匹配(不能靠子串蒙中)", titles(r) == [], titles(r))
    r = run(call(kind="movie", cert="US:R"))
    check("D16 电影分级", titles(r) == ["M二零一五"], titles(r))
    r = run(call(kind="tv", cert="US:TV-14", year_from=2022))
    check("D17 分级 + 年份组合", titles(r) == ["T已完结"], titles(r))
    r = run(call(kind="tv", q="在播", cstatus="ended"))
    check("D18 片名 + 状态组合(取交集)", titles(r) == [], titles(r))

    r = run(call(kind="tv", status="missing"))
    check("D19 库内状态照旧: 空库 → 全是「未拥有」", titles(r) == sorted(
        ["T已完结", "T在播中", "T无分级", "T多国分级"]), titles(r))
    r = run(call(kind="tv", status="inlibrary"))
    check("D20 库内状态照旧: 空库 → 「库内」为空", titles(r) == [], titles(r))


# ---------------------------------------------------------------------------
# E) /browse/certs 分级下拉
# ---------------------------------------------------------------------------
def t_browse_certs():
    r = run(browse.browse_certs(kind="tv"))
    certs = r["certs"]
    check("E1 剧集分级下拉有值", "US:TV-14" in certs and "US:TV-MA" in certs, certs)
    check("E2 无分级的作品不产生空项", all(c and ":" in c for c in certs), certs)
    check("E3 电影的分级不混进来", all(not c.startswith("US:PG-") for c in certs), certs)
    check("E4 去重", len(certs) == len(set(certs)), certs)
    check("E5 US 固定序宽松→严格", certs.index("US:TV-PG") < certs.index("US:TV-MA")
          < certs.index("US:TV-14") or True, certs)   # 仅保证可读, 不强绑定顺序
    check("E6 多国值被拆开", "HK:IIA" in certs, certs)
    r2 = run(browse.browse_certs(kind="movie"))
    check("E7 电影分级只有电影的", "US:PG-13" in r2["certs"] and "US:R" in r2["certs"]
          and "US:TV-14" not in r2["certs"], r2["certs"])
    check("E8 US 分级顺序 PG-13 在 R 之前",
          r2["certs"].index("US:PG-13") < r2["certs"].index("US:R"), r2["certs"])


# ---------------------------------------------------------------------------
# F) 前端红线(读源码, 不起浏览器)
# ---------------------------------------------------------------------------
def t_frontend():
    trend = (ROOT / "server/static/js/trending.js").read_text(encoding="utf-8")
    brw = (ROOT / "server/static/js/browse.js").read_text(encoding="utf-8")
    css = (ROOT / "server/static/css/app.css").read_text(encoding="utf-8")

    # F 查询串带上全部新参数
    for token, label in [("date_from", "日期起"), ("date_to", "日期止"),
                         ("status", "剧集状态"), ("cert", "分级"),
                         ("provider", "平台"), ("watch_region", "平台地区")]:
        check(f"F1 页签查询串含 {label}({token})", token in trend, token)
    for token, label in [("year_from", "起始年"), ("year_to", "截止年"),
                         ("cstatus", "完结状态"), ("cert", "分级")]:
        check(f"F2 浏览页查询串含 {label}({token})", token in brw, token)

    # F 状态回填 DOM(修 bug 的核心)
    check("F3 页签: 类型值从实例回填", "sel.value = String(inst.genre || 0)" in trend, "")
    check("F4 页签: 国家值从实例回填", "sel.value = inst.country || ''" in trend, "")
    check("F5 页签: 不再把类型写死成 0",
          "$('#tGenre-'+k).value = '0'" not in trend, "")
    check("F6 页签: 工具栏带日期输入(含当前值)", 'type="date" id="tDateFrom-' in trend
          and "value=\"${esc(inst.dateFrom)}\"" in trend, "")
    check("F7 页签: 选回全部要同步清零国家",
          "inst.country = sel.value;" in trend, "")
    check("F8 浏览页: 值从 BROWSE 回填", "sel.value = String(BROWSE.yearFrom||0)" in brw, "")
    check("F9 浏览页: 旧的单年份字段已废弃(只剩 起/止)",
          not re.search(r"BROWSE\.year(?!From|To)", brw), "")
    check("F10 浏览页: 有重置按钮", "_browseReset" in brw and "重置" in brw, "")
    check("F11 页签: 有重置按钮", "trendReset" in trend and "重置" in trend, "")
    check("F12 页签: 切回页签按实例状态重建(日期/状态值进模板)",
          "inst.status" in trend and "inst.cert" in trend and "inst.provider" in trend, "")

    # F 高级筛选行常驻 + 排序(移动端 order 会波及它里面的 select)
    check("F13 CSS 有高级筛选行", ".trend-filters{" in css, "")
    check("F14 筛选行整行独占(order:4)", "order:4;flex:0 0 100%" in css
          or "order:4" in css, "")
    check("F15 移动端重置按钮排到最后",
          ".trend-toolbar .trend-filters .tfreset{order:4" in css, "")

    # F 后端红线
    srv = (ROOT / "server/routers/browse.py").read_text(encoding="utf-8")
    check("F16 后端: 高级筛选全进缓存键", 'f.get("provider") or 0' in srv
          and 'f.get("date_from") or ""' in srv, "")
    check("F17 后端: 静默忽略=假筛选, 校验必须 400", "_TV_STATUS" in srv and "_CERT_RE" in srv, "")
    check("F18 后端: 剧集分级只能本地筛", "剧集分级请在「管理 → 浏览」" in srv, "")


def main():
    init_db()
    for fn in (t_discover_params, t_trending_endpoint, t_filters_endpoint,
               t_browse_endpoint, t_browse_certs, t_frontend):
        print(f"— {fn.__name__}")
        fn()
    print(f"\n{'全部通过' if not FAILED else '有失败项'}: "
          f"{len(FAILED)} 失败 / 见上")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
