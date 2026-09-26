#!/usr/bin/env python3
"""移动端导航栏可见性检查: 遍历各页签, 检测底部 nav.tabs 是否在视口内且未被覆盖。"""
import asyncio, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT))
from lib.config import load_config  # noqa: E402  统一配置入口(数据库优先)
BASE = "http://127.0.0.1:8787"

# 4 主页签 + 管理子页签
TABS = ["movie", "tv", "organize",
        "manage/missing", "manage/general", "manage/track",
        "manage/local", "manage/jobs", "manage/browse", "manage/block"]

async def goto_tab(page, tab):
    if tab.startswith("manage/"):
        sub = tab.split("/", 1)[1]
        await page.evaluate("switchTab('manage')")
        await page.wait_for_timeout(350)
        await page.evaluate(f"switchManageSub('{sub}')")
    else:
        await page.evaluate(f"switchTab('{tab}')")

def creds():
    cfg = load_config()
    a = cfg["web"]["auth"]
    return a["username"], a["password"]

async def login(page):
    await page.goto(BASE, wait_until="networkidle")
    if await page.locator("#login").is_visible():
        await page.fill("#lu", creds()[0])
        await page.fill("#lp", creds()[1])
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(800)

async def check_nav(page):
    return await page.evaluate("""() => {
        const nav = document.querySelector('nav.tabs');
        if(!nav) return {exists:false};
        const r = nav.getBoundingClientRect();
        const vh = window.innerHeight, vw = window.innerWidth;
        const inViewport = r.bottom <= vh + 1 && r.top >= -1 && r.width > 0;
        // 底部 3 个采样点, 看最顶层元素是否落在 nav 内
        const xs = [vw*0.2, vw*0.5, vw*0.8];
        const y = vh - 8;
        const coverInfo = xs.map(x => {
            const el = document.elementFromPoint(x, y);
            if(!el) return {x:Math.round(x), top:'null'};
            let inNav = false, p = el;
            while(p){ if(p === nav){ inNav = true; break; } p = p.parentElement; }
            return {x:Math.round(x), tag:el.tagName.toLowerCase(), cls:(el.className||'').toString().slice(0,30), inNav};
        });
        const covered = coverInfo.some(c => !c.inNav);
        const disp = getComputedStyle(nav).display;
        const modalShown = document.querySelector('#modalBg').classList.contains('show');
        return {exists:true, display:disp, rect:{top:Math.round(r.top),bottom:Math.round(r.bottom),height:Math.round(r.height)}, vh, inViewport, modalShown, covered, coverInfo};
    }""")

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome")
        ctx = await browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=3, is_mobile=True, has_touch=True)
        page = await ctx.new_page()
        page.on("pageerror", lambda e: print("  PAGEERR", e))
        await login(page)
        if await page.locator("#login").is_visible():
            print("登录失败"); await ctx.close(); return
        print(f"{'TAB':<18} {'disp':<6} {'rect.bottom':<12} {'inVP':<6} {'modal':<6} {'COVERED'}")
        print("-"*70)
        for tab in TABS:
            await goto_tab(page, tab)
            await page.wait_for_timeout(900)
            r = await check_nav(page)
            flag = "❌被覆盖" if r.get("covered") else "✓"
            print(f"{tab:<18} {r.get('display','?'):<6} {str(r.get('rect',{}).get('bottom','?')):<12} {str(r.get('inViewport')):<6} {str(r.get('modalShown')):<6} {flag}")
            if r.get("covered"):
                for c in r.get("coverInfo",[]):
                    if not c.get("inNav"):
                        print(f"     覆盖点 x={c['x']}: <{c['tag']} class='{c['cls']}'>")
        # 额外: 搜索页(模态) + 详情页(模态)
        print("-"*70)
        print("额外检查: 搜索页 / 详情页(应为模态覆盖 nav, 设计如此)")
        await page.evaluate("switchTab('movie')")
        await page.wait_for_timeout(700)
        await page.fill("#globSearch", "复仇者联盟")
        await page.evaluate("freeSearch()")
        await page.wait_for_timeout(1200)
        r = await check_nav(page)
        print(f"{'search-modal':<18} modal={r.get('modalShown')} covered={r.get('covered')}  (期望 modal=true)")
        await ctx.close()
        await browser.close()

asyncio.run(main())
