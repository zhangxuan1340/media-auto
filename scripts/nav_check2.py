#!/usr/bin/env python3
"""移动端导航栏: 模拟真机安全区 + 模态开关流程, 定位 nav 消失场景。"""
import asyncio, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"

def creds():
    cfg = json.loads((ROOT / "config.json").read_text())
    a = cfg["web"]["auth"]
    return a["username"], a["password"]

async def login(page):
    await page.goto(BASE, wait_until="networkidle")
    if await page.locator("#login").is_visible():
        await page.fill("#lu", creds()[0]); await page.fill("#lp", creds()[1])
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(800)

async def nav_state(page):
    return await page.evaluate("""() => {
        const nav = document.querySelector('nav.tabs');
        const r = nav.getBoundingClientRect();
        const vh = window.innerHeight, vw = window.innerWidth;
        const xs=[vw*0.2,vw*0.5,vw*0.8]; const y=vh-8;
        const cov = xs.map(x=>{const el=document.elementFromPoint(x,y);let p=el,inN=false;while(p){if(p===nav){inN=true;break;}p=p.parentElement;}return inN;});
        return {bottom:Math.round(r.bottom), vh, inVP:r.bottom<=vh+1&&r.top>=-1, modal:document.querySelector('#modalBg').classList.contains('show'), covered:cov.some(c=>!c), bodyOverflow:getComputedStyle(document.body).overflow};
    }""")

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome")
        ctx = await browser.new_context(viewport={"width":390,"height":844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        page = await ctx.new_page()
        page.on("pageerror", lambda e: print("  PAGEERR", e))
        # 模拟真机安全区(刘海 47 / Home 指示条 34)
        await page.add_init_script("()=>{const s=document.documentElement.style;s.setProperty('--sat','47px');s.setProperty('--sab','34px');}")
        await login(page)
        if await page.locator("#login").is_visible():
            print("登录失败"); return

        print("== 真机安全区(sat=47 sab=34)下各主页签 nav 状态 ==")
        for tab in ["movie","tv","organize","manage/browse","manage/local"]:
            if tab.startswith("manage/"):
                await page.evaluate("switchTab('manage')"); await page.wait_for_timeout(300)
                await page.evaluate(f"switchManageSub('{tab.split('/')[1]}')")
            else:
                await page.evaluate(f"switchTab('{tab}')")
            await page.wait_for_timeout(900)
            r=await nav_state(page)
            print(f"  {tab:<16} bottom={r['bottom']}/{r['vh']} inVP={r['inVP']} covered={r['covered']} modal={r['modal']}")

        print("\n== 模态开关流程: 打开详情→关闭→nav 是否恢复 ==")
        await page.evaluate("switchTab('movie')"); await page.wait_for_timeout(800)
        card = page.locator("#tab-movie .card").first
        if await card.count():
            await card.click(); await page.wait_for_timeout(1200)
            r=await nav_state(page); print(f"  打开详情: modal={r['modal']} covered={r['covered']} (期望 covered=True, 模态盖住 nav)")
            await page.evaluate("closeModal()"); await page.wait_for_timeout(500)
            r=await nav_state(page); print(f"  关闭详情: modal={r['modal']} covered={r['covered']} inVP={r['inVP']} bodyOverflow={r['bodyOverflow']}  (期望 covered=False, nav 恢复)")
        else:
            print("  无卡片可点")

        print("\n== 模态开关流程: 搜索页→关闭→nav 是否恢复 ==")
        await page.fill("#globSearch","盗梦空间"); await page.evaluate("freeSearch()"); await page.wait_for_timeout(1200)
        r=await nav_state(page); print(f"  打开搜索: modal={r['modal']} covered={r['covered']}")
        await page.evaluate("_navBack()"); await page.wait_for_timeout(500)
        r=await nav_state(page); print(f"  关闭搜索: modal={r['modal']} covered={r['covered']} inVP={r['inVP']} bodyOverflow={r['bodyOverflow']}")

        await ctx.close(); await browser.close()

asyncio.run(main())
