#!/usr/bin/env python3
"""验证 2026-09-22 两处 UI 修复:
1) .overview-more 玻璃胶囊样式(与 .ext-link 同款)
2) .prompt-bg 无 backdrop-filter(修花屏抖动) + .prompt-card 圆角统一 28px
用法: venv/bin/python scripts/verify_prompt_more.py
"""
import asyncio, json, sys
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8787"
OUT = "state/ui_verify"

async def login(page):
    await page.goto(BASE, wait_until="networkidle")
    if await page.locator("#login").is_visible():
        cfg = json.load(open("config.json"))
        a = cfg["web"]["auth"]
        await page.fill("#lu", a["username"])
        await page.fill("#lp", a["password"])
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(1200)

async def open_detail_with_more(page):
    """逐个打开热门卡片, 找到带「展开全部」的详情(简介>180字)。全程 JS click。"""
    for kind in ("movie", "tv"):
        await page.evaluate(f"switchTab('{kind}')")
        await page.wait_for_timeout(2500)
        n = await page.evaluate(f"document.querySelectorAll('#tab-{kind} .card').length")
        for i in range(min(n, 12)):
            await page.evaluate(
                f"document.querySelectorAll('#tab-{kind} .card')[{i}].click()")
            await page.wait_for_timeout(2500)
            has = await page.evaluate("!!document.querySelector('#ovMore')")
            if has:
                return f"{kind}#{i}"
            await page.evaluate("typeof closeModal==='function' && closeModal()")
            await page.wait_for_timeout(600)
    return None

async def measure(page):
    more = await page.evaluate("""() => {
      const b = document.querySelector('#ovMore');
      if(!b) return null;
      const cs = getComputedStyle(b);
      return {bg:cs.backgroundColor, border:cs.borderTopWidth+' '+cs.borderTopColor,
              radius:cs.borderRadius, pad:cs.padding, display:cs.display};
    }""")
    # 打开屏蔽弹窗
    await page.evaluate("""() => {
      const btns=[...document.querySelectorAll('.dhead-actions .ghost')];
      const b=btns.find(x=>x.textContent.includes('屏蔽')); if(b) b.click();
    }""")
    await page.wait_for_timeout(700)
    prompt = await page.evaluate("""() => {
      const bg = document.querySelector('.prompt-bg'); if(!bg) return null;
      const cs = getComputedStyle(bg);
      const card = getComputedStyle(document.querySelector('.prompt-card'));
      return {bf:cs.backdropFilter, webkitBf:cs.webkitBackdropFilter,
              cardRadius:card.borderRadius, cardBg:card.backgroundColor,
              anim:card.animationName};
    }""")
    return more, prompt

async def main():
    errors = []
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        for name, vp in (("desktop", {"width":1440,"height":900}),
                         ("mobile", {"width":393,"height":852,"is_mobile":True,"dsf":2})):
            ctx = await b.new_context(viewport={"width":vp["width"],"height":vp["height"]},
                                      is_mobile=vp.get("is_mobile",False),
                                      device_scale_factor=vp.get("dsf",1))
            pg = await ctx.new_page()
            await login(pg)
            found = await open_detail_with_more(pg)
            if not found:
                errors.append(f"{name}: 未找到带展开全部的详情")
                continue
            more, prompt = await measure(pg)
            print(f"[{name}] 详情={found}")
            print(f"  overview-more: {more}")
            print(f"  prompt:        {prompt}")
            # 断言
            if not more or more["bg"] == "rgba(0, 0, 0, 0)" or more["radius"] != "999px":
                errors.append(f"{name}: overview-more 未生效玻璃胶囊样式: {more}")
            if not prompt:
                errors.append(f"{name}: 屏蔽弹窗未弹出")
            else:
                if prompt["bf"] and prompt["bf"] != "none":
                    errors.append(f"{name}: prompt-bg 仍有 backdrop-filter={prompt['bf']} (花屏根因未除)")
                expect_radius = "24px" if name == "mobile" else "28px"
                if prompt["cardRadius"] != expect_radius:
                    errors.append(f"{name}: prompt-card 圆角={prompt['cardRadius']} (应为 {expect_radius})")
            await pg.screenshot(path=f"{OUT}/{name}_prompt_more.png")
            await ctx.close()
        await b.close()
    if errors:
        print("\n❌ FAIL"); [print(" -", e) for e in errors]; sys.exit(1)
    print("\n✅ 全部通过")

asyncio.run(main())
