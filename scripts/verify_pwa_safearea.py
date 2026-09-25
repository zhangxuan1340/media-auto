#!/usr/bin/env python3
"""iOS standalone PWA 安全区布局验证

Chrome 里 env(safe-area-inset-*) 恒为 0, 无法直接验证 iPhone 上的表现。
本脚本通过覆盖 CSS 变量 --sat / --sab 模拟真机安全区, 量出:
  ① header 第一行是否被状态栏盖住(要求 header 内容起点 >= --sat)
  ② 底部 tabbar 是否贴到视口底边(要求 tabbar.bottom == 视口高)
  ③ 滚动到底时最后一张卡片能否完全露出 tabbar 之上
用法: venv/bin/python scripts/verify_pwa_safearea.py
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"
OUT = ROOT / "state" / "ui_verify" / "pwa_safearea"

# iPhone 14 Pro 近似值: 状态栏 59px / 底部 Home 指示条 34px; 视口 393x852
SAFE = [
    {"name": "iphone_notch", "w": 393, "h": 852, "sat": 59, "sab": 34, "dpr": 3},
    {"name": "iphone_se",    "w": 375, "h": 667, "sat": 20, "sab": 0,  "dpr": 2},
    {"name": "android",      "w": 412, "h": 915, "sat": 24, "sab": 0,  "dpr": 2.6},
]

MEASURE = r"""
([sat, sab]) => {
  const H = innerHeight, W = innerWidth;
  const r = (sel) => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect() : null; };
  const header = r('header'), tabs = r('nav.tabs'), main = r('main');
  const logo = r('header .logo');
  const btns = [...document.querySelectorAll('header button')].map(b => b.getBoundingClientRect());
  const input = r('#globSearch');
  const firstRowTop = Math.min(...[logo, ...btns].filter(Boolean).map(x => x.top));
  const firstRowBottom = Math.max(...[logo, ...btns].filter(Boolean).map(x => x.bottom));

  // 横向溢出
  const over = [];
  for (const el of document.querySelectorAll('#app *')) {
    const b = el.getBoundingClientRect();
    if (b.width > 0 && (b.right > W + 1 || b.left < -1)) {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      let anc = el.parentElement, scrollable = false;
      while (anc && anc !== document.body) {
        const ax = getComputedStyle(anc).overflowX;
        if (ax === 'auto' || ax === 'scroll') { scrollable = true; break; }
        anc = anc.parentElement;
      }
      if (!scrollable) over.push(el.className || el.tagName);
    }
  }

  return {
    viewport: {w: W, h: H},
    sat: sat, sab: sab,
    header: header && {top: Math.round(header.top), bottom: Math.round(header.bottom),
                       padTop: getComputedStyle(document.querySelector('header')).paddingTop},
    firstRow: {top: Math.round(firstRowTop), bottom: Math.round(firstRowBottom)},
    firstRowClearsStatusBar: firstRowTop >= sat - 0.5,
    input: input && {top: Math.round(input.top), bottom: Math.round(input.bottom)},
    tabs: tabs && {top: Math.round(tabs.top), bottom: Math.round(tabs.bottom),
                   left: Math.round(tabs.left), right: Math.round(tabs.right),
                   padBottom: getComputedStyle(document.querySelector('nav.tabs')).paddingBottom},
    tabsFlushBottom: tabs ? Math.abs(tabs.bottom - H) <= 0.5 : null,
    headerBottomVsStatusBar: header ? Math.round(header.bottom) : null,
    mainPadBottom: main ? getComputedStyle(document.querySelector('main')).paddingBottom : null,
    hOverflow: over,
  };
}
"""

BOTTOM_CLEARANCE = r"""
() => {
  // 无需真的滚到底(热门榜是无限滚动): 直接比较"内容区底部留白"与"tabbar 实际高度"。
  // main 的 padding-bottom >= tabbar 高度, 才能保证滚到底时最后一条内容能完全露出 tabbar 之上。
  const main = document.querySelector('main');
  const tabs = document.querySelector('nav.tabs');
  const cs = getComputedStyle(main);
  const pad = parseFloat(cs.paddingBottom) || 0;
  const tb = tabs.getBoundingClientRect().height;
  return {
    mainPadBottom: Math.round(pad),
    tabbarHeight: Math.round(tb),
    clearance: Math.round(pad - tb),
    enough: pad >= tb,
  };
}
"""


async def main():
    from playwright.async_api import async_playwright
    cfg = json.loads((ROOT / "config.json").read_text())
    a = cfg["web"]["auth"]
    OUT.mkdir(parents=True, exist_ok=True)
    bad = []

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome")
        for dev in SAFE:
            ctx = await b.new_context(viewport={"width": dev["w"], "height": dev["h"]},
                                      device_scale_factor=dev["dpr"],
                                      is_mobile=True, has_touch=True)
            pg = await ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            await pg.goto(BASE, wait_until="networkidle")
            if await pg.locator("#login").is_visible():
                await pg.fill("#lu", a["username"])
                await pg.fill("#lp", a["password"])
                await pg.locator("#loginForm button[type=submit]").click()
                await pg.wait_for_timeout(900)
            # 模拟真机安全区
            await pg.evaluate(f"() => {{ document.documentElement.style.setProperty('--sat','{dev['sat']}px');"
                              f"document.documentElement.style.setProperty('--sab','{dev['sab']}px'); }}")
            await pg.evaluate("switchTab('tv')")
            await pg.wait_for_timeout(2500)

            m = await pg.evaluate(MEASURE, [dev["sat"], dev["sab"]])
            t = await pg.evaluate(BOTTOM_CLEARANCE)
            shot = OUT / f"{dev['name']}_tv.png"
            await pg.screenshot(path=str(shot))

            print(f"\n=== {dev['name']}  {dev['w']}x{dev['h']}  sat={dev['sat']} sab={dev['sab']} ===")
            print(f"  header: top={m['header']['top']} bottom={m['header']['bottom']} padTop={m['header']['padTop']}")
            print(f"  header 首行: {m['firstRow']['top']}~{m['firstRow']['bottom']}"
                  f"  让开状态栏? {'OK' if m['firstRowClearsStatusBar'] else 'FAIL (被状态栏盖住)'}")
            print(f"  底部 tabbar: top={m['tabs']['top']} bottom={m['tabs']['bottom']}"
                  f"  视口高={dev['h']}  贴底? {'OK' if m['tabsFlushBottom'] else 'FAIL'}"
                  f"  左右={m['tabs']['left']}~{m['tabs']['right']} padBottom={m['tabs']['padBottom']}")
            print(f"  底部留白: main padding-bottom={t['mainPadBottom']} - tabbar 高={t['tabbarHeight']}"
                  f" = 余量 {t['clearance']}px  {'OK' if t['enough'] else 'FAIL 内容会被遮'}")
            print(f"  横向溢出: {m['hOverflow'] if m['hOverflow'] else '无'}")
            if errs:
                print(f"  JS 错误: {errs}")

            if not m["firstRowClearsStatusBar"]:
                bad.append(f"{dev['name']}: header 首行被状态栏盖住")
            if not m["tabsFlushBottom"]:
                bad.append(f"{dev['name']}: tabbar 未贴底")
            if not t["enough"]:
                bad.append(f"{dev['name']}: 底部留白不足, 内容会被 tabbar 遮挡")
            if m["hOverflow"]:
                bad.append(f"{dev['name']}: 横向溢出 {m['hOverflow']}")
            if errs:
                bad.append(f"{dev['name']}: JS 错误")

            print(f"  截图 {shot}")
            await ctx.close()
        await b.close()

    print("\n" + "=" * 60)
    if bad:
        print("FAIL:")
        for x in bad:
            print("  -", x)
        return 1
    print("PASS: 三种设备下 header 让开状态栏 / tabbar 贴底 / 无遮挡 / 无横向溢出")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
