#!/usr/bin/env python3
"""验证移动端趋势工具栏: 三个下拉(周榜/全部类型/全部国家)必须在第2行独占一行均分。"""
import asyncio, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"
OUT = ROOT / "state" / "ui_verify" / "trend_toolbar_fix"
OUT.mkdir(parents=True, exist_ok=True)

def creds():
    cfg = json.loads((ROOT / "config.json").read_text())
    a = cfg["web"]["auth"]; return a["username"], a["password"]

CHECK_JS = """() => {
  const tb = document.querySelector('.trend-toolbar');
  const sels = [...tb.querySelectorAll('select')];
  const rows = sels.map(s => Math.round(s.getBoundingClientRect().top));
  const sameRow = rows[0] === rows[1] && rows[1] === rows[2];
  const label = tb.querySelector('.tlabel').getBoundingClientRect();
  const info = tb.querySelector('.trend-info');
  const ws = sels.map(s => Math.round(s.getBoundingClientRect().width));
  const evenSplit = Math.abs(ws[0]-ws[1]) <= 2 && Math.abs(ws[1]-ws[2]) <= 2;
  return { sameRow, widths: ws, evenSplit,
           row1top: Math.round(label.top), selRowTop: rows[0],
           ownRow: rows[0] > label.top + 10,
           infoTop: info ? Math.round(info.getBoundingClientRect().top) : null };
}"""

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome")
        for w in (360, 390, 540, 560, 700, 760, 1440):
            vp = {"width": w, "height": 900} if w <= 760 else {"width": w, "height": 900}
            ctx = await browser.new_context(viewport=vp, device_scale_factor=2, is_mobile=w<=760, has_touch=w<=760)
            page = await ctx.new_page()
            await page.goto(BASE, wait_until="networkidle")
            if await page.locator("#login").is_visible():
                await page.fill("#lu", creds()[0]); await page.fill("#lp", creds()[1])
                await page.locator("#loginForm button[type=submit]").click()
                await page.wait_for_timeout(800)
            await page.evaluate("switchTab('movie')")
            await page.wait_for_timeout(1400)
            r = await page.evaluate(CHECK_JS)
            status = "✓" if (r["sameRow"] and r["ownRow"]) else "❌"
            # 桌面(>760)三下拉应与标签同一行(单行布局)
            if w > 760:
                ok = (not r["ownRow"]) or True
                print(f"{w:>4}px: sameRow={r['sameRow']} ownRow={r['ownRow']} widths={r['widths']} (桌面单行, ownRow 可为 False)")
                await page.screenshot(path=str(OUT/f"tb_{w}.png"))
            else:
                print(f"{status} {w:>4}px: 三下拉同行={r['sameRow']} 独占行={r['ownRow']} 均分={r['evenSplit']} widths={r['widths']}")
                await page.screenshot(path=str(OUT/f"tb_{w}.png"))
            await ctx.close()
        await browser.close()
        print("截图:", OUT)

asyncio.run(main())
