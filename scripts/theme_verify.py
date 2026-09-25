#!/usr/bin/env python3
"""夜间模式 + 液态玻璃 tabbar + 玻璃推送按钮 专项验证(playwright)

用法:
  venv/bin/python scripts/theme_verify.py

覆盖:
  - 强制 浅色 / 深色 两套主题各截一遍
  - 移动端: 电影页(底部 tabbar 液态玻璃)、详情页(玻璃推送按钮)、通用页(夜间模式分段)
  - 桌面端: 电影页(顶部 tabbar)、通用页(夜间模式分段)
  - 校验 <html data-theme> 与 meta theme-color 同步
  - 横向溢出检查(移动端)
退出码: 0=无 JS 运行时错误, 1=有错误
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"

VIEWPORTS = {
    "mobile": {"width": 390, "height": 844, "device_scale_factor": 3, "is_mobile": True},
    "desktop": {"width": 1440, "height": 900, "device_scale_factor": 2},
}


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


async def set_theme(page, pref):
    """强制主题: 写 localStorage 后 reload(触发 <head> 内联脚本应用)"""
    await page.evaluate(f"localStorage.setItem('media_theme', '{pref}')")
    await page.reload(wait_until="networkidle")
    await page.wait_for_timeout(500)
    if await page.locator("#login").is_visible():
        await page.fill("#lu", creds()[0])
        await page.fill("#lp", creds()[1])
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(800)


async def check_theme_sync(page):
    return await page.evaluate("""() => {
        const th = document.documentElement.getAttribute('data-theme');
        const m = document.querySelector('meta[name="theme-color"]');
        const tc = m ? m.getAttribute('content') : null;
        return {theme: th, themeColor: tc};
    }""")


async def check_overflow(page, maxW):
    return await page.evaluate("""(maxW) => {
        const out = [];
        const els = document.querySelectorAll('#app *, #modalBg *');
        for (const el of els) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > maxW + 4) {
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                let inScroller = false;
                for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
                    const ox = getComputedStyle(a).overflowX;
                    if (ox === 'auto' || ox === 'scroll') { inScroller = true; break; }
                }
                if (inScroller) continue;
                out.push(`${el.tagName.toLowerCase()}${el.id ? '#'+el.id : ''} right=${Math.round(r.right)}`);
            }
        }
        return out.slice(0, 20);
    }""", maxW)


async def main():
    from playwright.async_api import async_playwright

    outdir = ROOT / "state" / "ui_verify" / "theme"
    outdir.mkdir(parents=True, exist_ok=True)
    errors_all = []

    async with async_playwright() as pw:
        browser = None
        for launch_kw in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(**launch_kw)
                break
            except Exception:
                if launch_kw:
                    continue
        if browser is None:
            print("❌ 无可用浏览器"); return 1

        for theme in ("light", "dark"):
            for vp_name, vp in VIEWPORTS.items():
                ctx = await browser.new_context(
                    viewport={"width": vp["width"], "height": vp["height"]},
                    device_scale_factor=vp.get("device_scale_factor", 2),
                    is_mobile=vp.get("is_mobile", False),
                    has_touch=vp.get("is_mobile", False),
                )
                page = await ctx.new_page()
                page_errors = []
                page.on("pageerror", lambda e: page_errors.append(str(e)))

                print(f"\n===== {theme} / {vp_name} =====")
                await login(page)
                await set_theme(page, theme)

                sync = await check_theme_sync(page)
                print(f"  data-theme={sync['theme']}  theme-color={sync['themeColor']}")
                expect = "dark" if theme == "dark" else "light"
                if sync["theme"] != expect:
                    print(f"  ❌ data-theme 期望 {expect} 实得 {sync['theme']}")
                    errors_all.append(f"{theme}/{vp_name} theme-mismatch")

                vdir = outdir / f"{vp_name}_{theme}"
                vdir.mkdir(parents=True, exist_ok=True)

                # 电影页(tabbar 可见)
                await page.evaluate("switchTab('movie')")
                await page.wait_for_timeout(1200)
                await page.screenshot(path=str(vdir / "movie.png"))
                print(f"  截图 {vdir}/movie.png")

                if vp_name == "mobile":
                    over = await check_overflow(page, vp["width"])
                    if over:
                        for o in over:
                            print(f"  ⚠️ [movie] 横向溢出: {o}")
                        errors_all.append(f"{theme}/{vp_name}/movie overflow")

                # 详情页(玻璃推送按钮)
                first_card = page.locator("#tab-movie .card").first
                if await first_card.count():
                    await first_card.click()
                    await page.wait_for_timeout(1500)
                    await page.screenshot(path=str(vdir / "detail.png"))
                    print(f"  截图 {vdir}/detail.png")
                    await page.evaluate("closeModal()")
                    await page.wait_for_timeout(400)

                # 通用页(夜间模式分段)
                await page.evaluate("switchTab('manage')")
                await page.wait_for_timeout(400)
                await page.evaluate("switchManageSub('general')")
                await page.wait_for_timeout(900)
                await page.screenshot(path=str(vdir / "general.png"))
                print(f"  截图 {vdir}/general.png")

                if page_errors:
                    for e in page_errors:
                        print(f"  ❌ [pageerror] {e[:200]}")
                    errors_all.append(f"{theme}/{vp_name} pageerror")
                else:
                    print("  ✓ 无 JS 运行时错误")

                await ctx.close()

        await browser.close()

    print("\n" + "=" * 50)
    if errors_all:
        print(f"❌ 共 {len(errors_all)} 类问题: {errors_all}")
        return 1
    print("✓ 主题验证通过: 数据主题同步 + 无 JS 错误 + 无横向溢出")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
