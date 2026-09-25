#!/usr/bin/env python3
"""竞态 bug 压力测试 + 玻璃效果验证。

场景复现: 反复快速切换 trending 页签(在 loadTrending 的 await /api/settings 窗口内
触发滚动监听抢跑), 检查 #trendBody 是否出现"加载热门榜…"(empty) 与"加载中…"(trend-foot)
两行并存。旧代码会命中, 修复后应为 0 次。

同时校验液态玻璃: 统计应用了 backdrop-filter 的元素数(>0 才算玻璃生效)。
"""
import asyncio, json, sys
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
        await page.fill("#lu", creds()[0])
        await page.fill("#lp", creds()[1])
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(800)


CHECK = """() => {
    const body = document.getElementById('trendBody');
    if (!body) return {noBody: true, empty: 0, foot: 0};
    const empty = body.querySelectorAll('.empty').length;   // 加载热门榜…/加载失败
    const foot = body.querySelectorAll('.trend-foot').length; // 加载中…/滚动加载更多
    const cards = body.querySelectorAll('.card').length;
    return {noBody:false, empty, foot, cards,
            both: (empty>0 && foot>0), // 双行并存 = bug
            multiFoot: foot>1};       // 多个加载行 = 累加 bug
}"""


async def main():
    from playwright.async_api import async_playwright
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    async with async_playwright() as pw:
        browser = None
        for kw in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(**kw); break
            except Exception:
                pass
        if not browser:
            print("无可用浏览器"); return 1
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = await ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        await login(page)
        if await page.locator("#login").is_visible():
            print("登录失败"); return 1

        # 1) 竞态压力: 反复切 trending/missing/browse, 每次切到 trending 后短间隔探测
        both_hits, multi_hits = 0, 0
        for i in range(n):
            await page.evaluate("switchTab('missing')")
            await page.wait_for_timeout(120)
            await page.evaluate("switchTab('browse')")
            await page.wait_for_timeout(80)
            # 切回 trending → loadTrending 的 await 窗口
            await page.evaluate("switchTab('trending')")
            for _ in range(6):
                st = await page.evaluate(CHECK)
                if st.get("both"):
                    both_hits += 1
                    print(f"  ❌ 第{i}轮 双行并存: {st}")
                if st.get("multiFoot"):
                    multi_hits += 1
                    print(f"  ❌ 第{i}轮 多加载行: {st}")
                await page.wait_for_timeout(40)
        print(f"\n竞态压力 {n} 轮: 双行并存={both_hits} 多加载行={multi_hits}")

        # 2) 滚到底验证自动加载不重复/不双行
        await page.evaluate("switchTab('trending')")
        await page.wait_for_timeout(1200)
        for _ in range(4):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(600)
            st = await page.evaluate(CHECK)
            if st.get("both") or st.get("multiFoot"):
                print(f"  ❌ 滚动加载时双行: {st}")
                both_hits += 1
        st = await page.evaluate(CHECK)
        print(f"  滚动后状态: cards={st['cards']} empty={st['empty']} foot={st['foot']} (foot 应≤1)")

        # 3) 玻璃效果: 统计 backdrop-filter 生效的元素
        glass = await page.evaluate("""() => {
            const els = document.querySelectorAll('header, nav.tabs, .card, .modal, .toast, .res, .login-card, .person-chip, select, table');
            let c = 0; const names = [];
            els.forEach(e => {
                const bf = getComputedStyle(e).backdropFilter || getComputedStyle(e).webkitBackdropFilter || '';
                if (bf && bf !== 'none') { c++; names.push(e.tagName.toLowerCase()+(e.className&&typeof e.className==='string'?'.'+e.className.trim().split(/\\s+/)[0]:'')); }
            });
            return {count: c, sample: names.slice(0,8)};
        }""")
        print(f"  玻璃元素数: {glass['count']}  示例: {glass['sample']}")

        # 4) 背景渐变是否生效(body 应有 radial-gradient)
        bg = await page.evaluate("() => { const b = getComputedStyle(document.body).backgroundImage; return (b||'').includes('radial-gradient'); }")
        print(f"  body 渐变背景: {'✓ 生效' if bg else '✗ 未生效'}")

        if errs:
            print("❌ pageerror:")
            for e in errs[:5]: print("  ", e[:160])
        else:
            print("  ✓ 全程无 JS 运行时错误")

        await ctx.close()
        await browser.close()

    ok = both_hits == 0 and not errs and glass["count"] > 0
    print("\n" + ("✓ 竞态修复验证通过 + 玻璃生效" if ok else "❌ 存在问题, 见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
