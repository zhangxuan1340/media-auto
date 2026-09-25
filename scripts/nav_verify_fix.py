#!/usr/bin/env python3
"""验证移动端底部导航在详情/搜索模态上也常驻: 截图 + 交互校验。"""
import asyncio, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"
OUT = ROOT / "state" / "ui_verify" / "mobile_navfix"
OUT.mkdir(parents=True, exist_ok=True)

def creds():
    cfg = json.loads((ROOT / "config.json").read_text())
    a = cfg["web"]["auth"]; return a["username"], a["password"]

async def login(page):
    await page.goto(BASE, wait_until="networkidle")
    if await page.locator("#login").is_visible():
        await page.fill("#lu", creds()[0]); await page.fill("#lp", creds()[1])
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(800)

async def nav_top_at_bottom(page):
    return await page.evaluate("""() => {
        const nav=document.querySelector('nav.tabs');
        const y=window.innerHeight-10, x=window.innerWidth/2;
        const el=document.elementFromPoint(x,y);
        let p=el,inN=false; while(p){if(p===nav){inN=true;break;}p=p.parentElement;}
        const cs=getComputedStyle(nav);
        return {navZ:cs.zIndex, modalZ:getComputedStyle(document.querySelector('#modalBg')).zIndex, topIsNav:inN, tag:el&&el.tagName};
    }""")

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome")
        ctx = await browser.new_context(viewport={"width":390,"height":844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        page = await ctx.new_page()
        await page.add_init_script("()=>{const s=document.documentElement.style;s.setProperty('--sat','47px');s.setProperty('--sab','34px');}")
        await login(page)
        if await page.locator("#login").is_visible(): print("登录失败"); return

        # 主页签: nav 应在底部且是最顶层
        await page.evaluate("switchTab('movie')"); await page.wait_for_timeout(900)
        r=await nav_top_at_bottom(page)
        print(f"电影主页签: navZ={r['navZ']} modalZ={r['modalZ']} 底部最顶层是nav={r['topIsNav']}")
        await page.screenshot(path=str(OUT/"01_movie_tab.png"))

        # 打开详情(模态)
        card = page.locator("#tab-movie .card").first
        if await card.count():
            await card.click(); await page.wait_for_timeout(1300)
            r=await nav_top_at_bottom(page)
            print(f"详情模态内: navZ={r['navZ']} modalZ={r['modalZ']} 底部最顶层是nav={r['topIsNav']}  (期望 topIsNav=True)")
            await page.screenshot(path=str(OUT/"02_detail_modal.png"))
            # 点底部"整理"页签 → 应关闭模态并切到整理
            await page.click('nav.tabs button[data-tab="organize"]')
            await page.wait_for_timeout(1000)
            st=await page.evaluate("""()=>({modal:document.querySelector('#modalBg').classList.contains('show'), organizeVisible:getComputedStyle(document.querySelector('#tab-organize')).display!=='none', active:document.querySelector('nav.tabs button.active')?.dataset.tab})""")
            print(f"点底部'整理'后: modal关={not st['modal']} 整理可见={st['organizeVisible']} 当前页签={st['active']}  (期望 关=True 整理可见=True active=organize)")
            await page.screenshot(path=str(OUT/"03_after_switch.png"))
        else:
            print("无卡片可点, 跳过模态测试")

        # 搜索模态
        await page.evaluate("switchTab('tv')"); await page.wait_for_timeout(700)
        await page.fill("#globSearch","盗梦空间"); await page.evaluate("freeSearch()"); await page.wait_for_timeout(1300)
        r=await nav_top_at_bottom(page)
        print(f"搜索模态内: 底部最顶层是nav={r['topIsNav']}  (期望 True)")
        await page.screenshot(path=str(OUT/"04_search_modal.png"))
        await ctx.close(); await browser.close()
        print("截图存于", OUT)

asyncio.run(main())
