#!/usr/bin/env python3
"""验证详情页地区显示: 港片应显"港片 · 香港", 欧美片应显"欧美"。"""
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

async def open_detail(page, kind, tmdb_id):
    await page.evaluate(f"openDetailLocal('{kind}', {tmdb_id})")
    await page.wait_for_selector("#mBody .dhead-facts", timeout=8000)
    await page.wait_for_timeout(400)
    # 读取"地区" fact 的值
    return await page.evaluate("""() => {
      const facts=[...document.querySelectorAll('#mBody .dhead-facts .fact')];
      const f=facts.find(x=>x.querySelector('.k')?.textContent==='地区');
      return f ? f.querySelector('.v').textContent.trim() : null;
    }""")

async def main():
    errors=[]
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        for name, vp, cases in (
            ("desktop", {"width":1440,"height":900}, [("movie",52776,"港片 · 香港"),("movie",27205,"欧美")]),
            ("mobile", {"width":393,"height":852,"is_mobile":True}, [("movie",52776,"港片 · 香港")]),
        ):
            ctx = await b.new_context(viewport={"width":vp["width"],"height":vp["height"]},
                                      is_mobile=vp.get("is_mobile",False))
            pg = await ctx.new_page()
            await login(pg)
            for kind, tid, expect in cases:
                got = await open_detail(pg, kind, tid)
                ok = got and got.startswith(expect.split(" ·")[0])
                print(f"[{name}] {kind}#{tid} 地区显示: {got!r} (期望含 {expect.split(' ·')[0]!r}) -> {'✓' if ok else '✗'}")
                if not ok:
                    errors.append(f"{name} {kind}#{tid}: 地区={got!r}")
                await pg.screenshot(path=f"{OUT}/{name}_region_{tid}.png")
                await pg.evaluate("typeof closeModal==='function' && closeModal()")
                await pg.wait_for_timeout(400)
            await ctx.close()
        await b.close()
    if errors:
        print("\n❌ FAIL"); [print(" -", e) for e in errors]; sys.exit(1)
    print("\n✅ 地区显示验证通过")

asyncio.run(main())
