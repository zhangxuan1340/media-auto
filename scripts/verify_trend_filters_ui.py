#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【纯本地】电影/剧集页签「高级筛选回显」UI 回归(playwright + 系统 Chrome)

对应 bug(2026-10-02 用户报障): 切到别的页签再回来, 列表**还是筛过的数据**,
筛选框却显示「全部」—— 根因是每次进页签都重建工具栏 HTML, 而状态留在实例里,
DOM 却被写成默认值(旧代码还只在选中项非空时写状态, 选回「全部」清不掉)。

本脚本起自己的服务(临时库 + 随机端口, 不碰 data/media_auto.db), 并把
`/api/**` 的 GET 全部在浏览器侧打桩(记录 /api/trending/{kind} 的查询串,
返回空榜单)—— 因此**不连网、不需要 tmdb.api_key**, 只验证前端行为:

  1) 工具栏长出 `.trend-filters` 行: 日期区间 / 分级 / 平台 / 平台地区 / 重置;
     电影没有状态下拉, 剧集有; 剧集的分级下拉置灰并注明去哪筛。
  2) 改筛选 → 请求串带 date_from/date_to/cert/provider/watch_region。
  3) 切走再切回 → 控件仍显示上次的值(= bug 修复), 且请求仍带这些参数。
  4) 有筛选时「日/周榜」下拉禁用; 点「重置」全清 + 请求串干净。
  5) 剧集页签的「已完结」状态独立保留(电影页签不串)。
  6) 移动端 390px 下这些控件都可见(没被挤出屏幕)。
  7) 全程无 JS 运行时错误。

用法: ./venv/bin/python scripts/verify_trend_filters_ui.py
退出码: 0 = 全部通过; 1 = 有失败项 / 服务起不来 / 没浏览器
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# 浏览器侧桩数据(只要形状对, 前端不会去挑内容)
# region/providers_err 是后端真实字段: region=实际生效的平台地区(默认 HK ——
# TMDB 没有 CN 的平台数据), providers_err 区分"该地区没有平台"和"没拉到"。
FILTERS_MOVIE = {
    "regions": [{"code": "HK", "name": "中国香港"}, {"code": "US", "name": "美国"}],
    "region": "HK",
    "providers": [{"id": 8, "name": "Netflix"}, {"id": 9, "name": "Prime Video"}],
    "providers_err": False,
    "certifications": [{"cert": "US:G"}, {"cert": "US:PG-13"}, {"cert": "US:R"}],
}
FILTERS_TV = {
    "regions": [{"code": "HK", "name": "中国香港"}, {"code": "US", "name": "美国"}],
    "region": "HK",
    "providers": [{"id": 8, "name": "Netflix"}, {"id": 9, "name": "Prime Video"}],
    "providers_err": False,
    "certifications": [],
}
# 用例 11: 切到一个 TMDB 没有平台表的地区 → providers 为空且 providers_err=False
STATE = {"no_data": False}
BROWSE_CERTS = {"tv": ["US:TV-PG", "US:TV-14", "US:TV-MA"], "movie": ["US:PG-13", "US:R"]}
BROWSE_YEARS = [2019, 2020, 2021, 2022]


async def _install_routes(page, hits):
    async def handler(route):
        req = route.request
        if req.method != "GET":
            await route.continue_()          # 登录等写请求走真服务
            return
        url = req.url
        path = urlparse(url).path
        qs = parse_qs(urlparse(url).query)

        async def body(obj):
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(obj, ensure_ascii=False))

        if path.startswith("/api/trending/filters"):
            kind = (qs.get("kind") or ["movie"])[0]
            payload = FILTERS_TV if kind == "tv" else FILTERS_MOVIE
            if STATE["no_data"]:
                payload = dict(payload, providers=[], providers_err=False)
            await body(payload)
        elif path.startswith("/api/trending/countries"):
            await body([{"code": "CN", "name": "中国"}, {"code": "US", "name": "美国"}])
        elif path.startswith("/api/trending/movie") or path.startswith("/api/trending/tv"):
            hits.append(path.rsplit("/", 1)[-1] + "?" + urlparse(url).query)
            await body({"items": [], "hasMore": False})
        elif path.startswith("/api/browse/genres"):
            await body([{"id": 28, "name": "动作"}, {"id": 18, "name": "剧情"}])
        elif path.startswith("/api/browse/years"):
            await body({"years": BROWSE_YEARS})
        elif path.startswith("/api/browse/certs"):
            kind = (qs.get("kind") or ["tv"])[0]
            await body({"certs": BROWSE_CERTS.get(kind, [])})
        elif path == "/api/browse":
            hits.append("browse?" + urlparse(url).query)
            await body({"items": [], "total": 0})
        elif path.startswith("/api/settings"):
            await body({"hide_complete": False})
        elif path.startswith("/api/config/setup"):
            # 桩返回"已完成"—— 否则首次引导遮罩 #setupOv 盖住整页, 点不到工具栏
            await body({"done": True, "checks": {}, "steps": []})
        else:
            await body({})

    await page.route("**/api/**", handler)


async def _login(page, base):
    await page.goto(base, wait_until="domcontentloaded")
    if await page.locator("#login").is_visible():
        await page.fill("#lu", "admin")
        await page.fill("#lp", "verify_pass_123")
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(900)


async def _sel(page, sel, value):
    """选值(下拉)后给首屏请求留出落地时间。"""
    await page.select_option(sel, value)
    await page.wait_for_timeout(450)


async def _last_trend(hits, kind, after=None):
    after = after if after is not None else 0
    for u in reversed(hits[after:]):
        if u.startswith(kind + "?"):
            return parse_qs(u.split("?", 1)[1])
    return None


async def _last_browse(hits, after=None):
    after = after if after is not None else 0
    for u in reversed(hits[after:]):
        if u.startswith("browse?"):
            return parse_qs(u.split("?", 1)[1])
    return None


async def _goto_tab(page, tab, hits, after=None):
    n = len(hits)
    await page.evaluate(f"switchTab('{tab}')")
    await page.wait_for_timeout(1200)
    return n


async def flow(base):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = None
        for launch_kw in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(**launch_kw)
                print(f"  浏览器: {'系统 Chrome' if launch_kw else '自带 chromium'}")
                break
            except Exception:                # noqa: BLE001
                continue
        if not browser:
            check("起浏览器", False, "系统 Chrome / chromium 都起不来")
            return

        # ---------- 桌面 ----------
        # service_workers='block': 注册了 SW 后 page.route 抓不到 API 请求
        # (playwright 不拦 SW 发出的请求), 桩会悄悄失效 → 筛选下拉变空。
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900},
                                        service_workers="block")
        page = await ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        hits = []
        await _install_routes(page, hits)
        await _login(page, base)

        n = await _goto_tab(page, "movie", hits)

        # 1) 工具栏长出高级筛选行
        check("1) 高级筛选行存在", await page.locator("#tab-movie .trend-filters").count() == 1)
        check("1) 日期区间两个输入", await page.locator("#tDateFrom-movie").count() == 1
              and await page.locator("#tDateTo-movie").count() == 1)
        check("1) 分级/平台/地区/重置齐全",
              await page.locator("#tCert-movie").count() == 1
              and await page.locator("#tProvider-movie").count() == 1
              and await page.locator("#tRegion-movie").count() == 1
              and await page.locator("#tab-movie .trend-filters .tfreset").count() == 1)
        # 两个"地区"必须分清: 第一行是产地国, 平台地区要带可见标签(不能是个裸国家名)
        lbl = page.locator("#tab-movie .trend-filters .tflbl")
        check("1) 平台地区有可见标签", await lbl.count() == 1
              and (await lbl.inner_text()).strip() == "平台地区" and await lbl.is_visible(),
              await lbl.inner_text() if await lbl.count() else "无")
        check("1) 产地国/平台地区各标各的",
              await page.locator("#tCountry-movie").get_attribute("aria-label") == "产地国"
              and await page.locator("#tRegion-movie").get_attribute("aria-label") == "平台地区", "")
        check("1) 电影没有状态下拉", await page.locator("#tStatus-movie").count() == 0)
        w = page.locator("#tWindow-movie")
        check("1) 无筛选时日/周榜可用", not await w.is_disabled())

        # 2) 改筛选 → 请求串带上参数
        await page.fill("#tDateFrom-movie", "2024-01-01")
        await page.dispatch_event("#tDateFrom-movie", "change")
        await page.wait_for_timeout(450)
        await page.fill("#tDateTo-movie", "2024-12-31")
        await page.dispatch_event("#tDateTo-movie", "change")
        await page.wait_for_timeout(450)
        await _sel(page, "#tCert-movie", "US:R")
        await _sel(page, "#tProvider-movie", "9")

        q = await _last_trend(hits, "movie")
        check("2) 请求带 date_from/date_to",
              bool(q) and q.get("date_from") == ["2024-01-01"] and q.get("date_to") == ["2024-12-31"], q)
        check("2) 请求带分级", bool(q) and q.get("cert") == ["US:R"], q)
        check("2) 请求带平台+地区", bool(q) and q.get("provider") == ["9"]
              and q.get("watch_region") == ["HK"], q)
        check("2) 有筛选时日/周榜禁用", await w.is_disabled())
        check("2) 重置按钮有高亮提示",
              await page.locator("#tab-movie .trend-filters .tfreset.on").count() == 1)

        # 3) 切走再切回 → 值必须回显(bug 本体)
        await _goto_tab(page, "tv", hits)
        await _goto_tab(page, "movie", hits)
        check("3) 切回后日期回显", await page.input_value("#tDateFrom-movie") == "2024-01-01"
              and await page.input_value("#tDateTo-movie") == "2024-12-31",
              (await page.input_value("#tDateFrom-movie"), await page.input_value("#tDateTo-movie")))
        check("3) 切回后分级回显", await page.input_value("#tCert-movie") == "US:R",
              await page.input_value("#tCert-movie"))
        check("3) 切回后平台回显", await page.input_value("#tProvider-movie") == "9",
              await page.input_value("#tProvider-movie"))
        q = await _last_trend(hits, "movie")
        check("3) 切回后请求仍是筛过的", bool(q) and q.get("date_from") == ["2024-01-01"]
              and q.get("cert") == ["US:R"], q)

        # 4) 重置
        n = len(hits)
        await page.locator("#tab-movie .trend-filters .tfreset").click()
        await page.wait_for_timeout(1000)
        check("4) 重置后日期清空", await page.input_value("#tDateFrom-movie") == ""
              and await page.input_value("#tDateTo-movie") == "", "")
        check("4) 重置后分级/平台回「全部」",
              await page.input_value("#tCert-movie") == ""
              and await page.input_value("#tProvider-movie") == "", "")
        q = await _last_trend(hits, "movie", after=n)
        check("4) 重置后请求串干净",
              bool(q) and not any(k in q for k in ("date_from", "date_to", "cert", "provider")), q)
        check("4) 重置后日/周榜恢复可用", not await w.is_disabled())

        # 5) 剧集: 状态筛选 + 剧集分级置灰 + 与电影页签互不串
        n = await _goto_tab(page, "tv", hits)
        check("5) 剧集有状态下拉", await page.locator("#tStatus-tv").count() == 1)
        dummy = page.locator("#tCertDummy-tv")
        check("5) 剧集分级置灰并注明去向",
              await dummy.count() == 1 and await dummy.is_disabled()
              and "浏览" in (await dummy.get_attribute("title") or ""),
              await dummy.get_attribute("title"))
        await _sel(page, "#tStatus-tv", "3")
        q = await _last_trend(hits, "tv")
        check("5) 剧集状态进请求", bool(q) and q.get("status") == ["3"], q)
        await _goto_tab(page, "movie", hits)
        check("5) 电影页签没被剧集状态污染", await page.locator("#tStatus-movie").count() == 0)
        await _goto_tab(page, "tv", hits)
        check("5) 剧集状态切回仍回显", await page.input_value("#tStatus-tv") == "3",
              await page.input_value("#tStatus-tv"))

        # 8) 管理 → 浏览: 年份范围 / 完结状态 / 分级 + 切走切回的回显(同款 bug)
        await _goto_tab(page, "movie", hits)
        n = len(hits)
        await page.evaluate("switchTab('manage','browse')")
        await page.wait_for_timeout(1500)
        check("8) 浏览页长出年份范围/完结状态/分级",
              await page.locator("#bYearFrom").count() == 1
              and await page.locator("#bYearTo").count() == 1
              and await page.locator("#bCstatus").count() == 1
              and await page.locator("#bCert").count() == 1)
        await _sel(page, "#bYearFrom", "2019")
        await _sel(page, "#bYearTo", "2022")
        await _sel(page, "#bCstatus", "ended")
        await _sel(page, "#bCert", "US:TV-14")
        bq = await _last_browse(hits, after=n)
        check("8) 请求带 year_from/year_to", bool(bq) and bq.get("year_from") == ["2019"]
              and bq.get("year_to") == ["2022"], bq)
        check("8) 请求带 cstatus/cert", bool(bq) and bq.get("cstatus") == ["ended"]
              and bq.get("cert") == ["US:TV-14"], bq)

        await page.evaluate("switchTab('movie')")
        await page.wait_for_timeout(900)
        await page.evaluate("switchTab('manage','browse')")
        await page.wait_for_timeout(1500)
        check("8) 切回后年份回显", await page.input_value("#bYearFrom") == "2019"
              and await page.input_value("#bYearTo") == "2022",
              (await page.input_value("#bYearFrom"), await page.input_value("#bYearTo")))
        check("8) 切回后完结状态/分级回显",
              await page.input_value("#bCstatus") == "ended"
              and await page.input_value("#bCert") == "US:TV-14",
              (await page.input_value("#bCstatus"), await page.input_value("#bCert")))
        bq = await _last_browse(hits)
        check("8) 切回后请求仍是筛过的", bool(bq) and bq.get("year_from") == ["2019"]
              and bq.get("cert") == ["US:TV-14"], bq)

        n = len(hits)
        await page.locator('#manageBody button[title*="清空"]').click()
        await page.wait_for_timeout(1200)
        check("9) 重置后全部清零",
              await page.input_value("#bYearFrom") == "0"
              and await page.input_value("#bYearTo") == "0"
              and await page.input_value("#bCstatus") == ""
              and await page.input_value("#bCert") == "",
              (await page.input_value("#bYearFrom"), await page.input_value("#bCert")))
        bq = await _last_browse(hits, after=n)
        check("9) 重置后请求串干净",
              bool(bq) and not any(k in bq for k in ("year_from", "year_to", "cstatus", "cert")), bq)

        # 换 kind → 另一个 kind 下未必存在的筛选要一并清零, 且完结状态不该出现在电影里
        await _sel(page, "#bKind", "movie")
        check("9) 电影没有完结状态下拉", await page.locator("#bCstatus").count() == 0)
        check("9) 换 kind 清掉上一个 kind 的筛选",
              await page.input_value("#bCert") == "" and await page.input_value("#bYearFrom") == "0",
              (await page.input_value("#bCert"), await page.input_value("#bYearFrom")))

        # ---------- 11) 该平台地区没有平台数据(TMDB 没有这张平台表) ----------
        await page.evaluate("switchTab('movie')")
        await page.wait_for_timeout(900)
        STATE["no_data"] = True
        await page.evaluate("loadTrendFilters('movie')")
        await page.wait_for_timeout(700)
        ps = page.locator("#tProvider-movie")
        check("11) 无平台数据 → 平台下拉只剩提示并禁用", await ps.is_disabled()
              and "暂无平台数据" in (await ps.first.inner_text()), await ps.first.inner_text())
        check("11) 无平台数据时已选平台归零", await ps.input_value() == "", await ps.input_value())
        STATE["no_data"] = False
        await page.evaluate("loadTrendFilters('movie')")
        await page.wait_for_timeout(700)
        check("11) 有数据时平台下拉恢复可用", not await ps.is_disabled(), "")

        check("6) 桌面无 JS 运行时错误", not errs, errs[:3])
        await ctx.close()

        # ---------- 移动端 390px ----------
        ctx2 = await browser.new_context(viewport={"width": 390, "height": 844},
                                         device_scale_factor=2, is_mobile=True, has_touch=True,
                                         service_workers="block")
        page2 = await ctx2.new_page()
        errs2 = []
        page2.on("pageerror", lambda e: errs2.append(str(e)))
        await _install_routes(page2, [])
        await _login(page2, base)
        await _goto_tab(page2, "movie", [])
        for sel in ("#tDateFrom-movie", "#tDateTo-movie", "#tCert-movie",
                    "#tProvider-movie", "#tRegion-movie"):
            check(f"7) 390px 可见 {sel}", await page2.locator(sel).is_visible())
        lbl2 = page2.locator("#tab-movie .trend-filters .tflbl")
        check("7) 390px 平台地区标签可见", await lbl2.count() == 1 and await lbl2.is_visible(),
              await lbl2.inner_text() if await lbl2.count() else "无")
        box = await page2.locator("#tab-movie .trend-filters").bounding_box()
        check("7) 390px 筛选行不横向溢出",
              bool(box) and box["x"] >= -1 and box["x"] + box["width"] <= 391, box)
        # 老口径不能被新筛选行打乱: 日榜/类型/国家 三下拉仍同行且独占一行
        tops = await page2.evaluate("""() => {
          const t = s => { const e = document.querySelector(s);
                           return e ? Math.round(e.getBoundingClientRect().top) : -1; };
          return [t('#tWindow-movie'), t('#tGenre-movie'), t('#tCountry-movie'),
                  t('#tab-movie .tlabel')];
        }""")
        check("10) 390px 老三下拉仍同行", len(set(tops[:3])) == 1, tops)
        check("10) 390px 老三下拉仍独占一行", tops[0] > tops[3] + 10, tops)
        check("7) 移动端无 JS 运行时错误", not errs2, errs2[:3])
        await ctx2.close()
        await browser.close()


def main():
    tmp = tempfile.mkdtemp(prefix="media_auto_trend_ui_")
    db = os.path.join(tmp, "verify.db")
    os.environ["MEDIA_AUTO_DB"] = db
    port = free_port()

    from lib import config as lc          # noqa: PLC0415
    cfg = lc.load_config()
    cfg.setdefault("web", {})
    cfg["web"]["host"] = "127.0.0.1"
    cfg["web"]["port"] = port
    cfg["web"]["auth"] = {"username": "admin", "password": "verify_pass_123"}
    lc.save_config(cfg)
    lc.set_setup_done(True)

    env = dict(os.environ, MEDIA_AUTO_DB=db)
    logf = open(os.path.join(tmp, "server.log"), "w")   # noqa: SIM115 失败时好排查
    srv = subprocess.Popen([sys.executable, "-m", "server.main"], cwd=str(ROOT),
                           env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        ready = False
        for _ in range(80):
            if srv.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/", timeout=2) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:                # noqa: BLE001
                time.sleep(0.4)
        check("服务起来了", ready, f"见 {tmp}/server.log")
        if ready:
            asyncio.run(flow(base))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:                    # noqa: BLE001
            srv.kill()
        logf.close()

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        print(f"  服务日志: {tmp}/server.log")
        return 1
    print("  全部通过")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
