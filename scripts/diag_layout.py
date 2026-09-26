#!/usr/bin/env python3
"""移动端布局体检(临时诊断脚本)

量出 header / nav.tabs / main / 第一张卡片 的实际矩形与计算样式,
并模拟 iOS safe-area 看会不会被顶栏/底栏遮挡。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT))
from lib.config import load_config  # noqa: E402  统一配置入口(数据库优先)
BASE = "http://127.0.0.1:8787"

PROBE = r"""
(cfg) => {
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {sel, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
            bottom: Math.round(r.bottom), pos: cs.position,
            padTop: cs.paddingTop, padBottom: cs.paddingBottom, z: cs.zIndex};
  };
  const out = {
    viewport: {w: innerWidth, h: innerHeight},
    header: box('header'),
    tabs: box('nav.tabs'),
    main: box('main'),
    firstCard: box('#tab-tv .card'),
    trendToolbar: box('#tab-tv .trend-toolbar'),
    tlabel: box('#tab-tv .tlabel'),
    trendInfo: box('#tab-tv .trend-info'),
    exitBtn: box('header button:last-of-type'),
    logo: box('header .logo'),
    globSearch: box('#globSearch'),
  };
  // 首屏可见文本, 判断 header 第一行是不是被顶到视口外
  out.headerRows = [...document.querySelectorAll('header > *')].map(el => {
    const r = el.getBoundingClientRect();
    return {tag: el.tagName + '.' + (el.className || ''), text: (el.textContent || '').trim().slice(0, 12),
            y: Math.round(r.y), h: Math.round(r.height), bottom: Math.round(r.bottom)};
  });
  return out;
}
"""


async def main():
    from playwright.async_api import async_playwright
    cfg = load_config()
    a = cfg["web"]["auth"]

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome")
        ctx = await b.new_context(viewport={"width": 390, "height": 844},
                                 device_scale_factor=3, is_mobile=True, has_touch=True)
        pg = await ctx.new_page()
        pg.on("console", lambda m: print("  [console]", m.type, m.text[:160])
              if m.type in ("error", "warning") else None)
        await pg.goto(BASE, wait_until="networkidle")
        if await pg.locator("#login").is_visible():
            await pg.fill("#lu", a["username"])
            await pg.fill("#lp", a["password"])
            await pg.locator("#loginForm button[type=submit]").click()
            await pg.wait_for_timeout(900)
        await pg.evaluate("switchTab('tv')")
        await pg.wait_for_timeout(2500)

        res = await pg.evaluate(PROBE, {})
        print(json.dumps(res, ensure_ascii=False, indent=2))

        # 滚到底, 看最后一张卡片能不能完全露出底栏之上(滚动容器可能是 #app 而非 window)
        await pg.evaluate("""() => {
          const sc = ['#app','main','.tabpane','body','html'];
          for (const s of sc) {
            const el = document.querySelector(s);
            if (el && el.scrollHeight > el.clientHeight + 8) {
              el.scrollTop = el.scrollHeight; return s;
            }
          }
          window.scrollTo(0, document.body.scrollHeight); return 'window';
        }""")
        await pg.wait_for_timeout(600)
        tail = await pg.evaluate("""() => {
          const findScroller = () => {
            for (const s of ['#app','main','.tabpane','body','html']) {
              const el = document.querySelector(s);
              if (el && el.scrollHeight > el.clientHeight + 8) return el;
            }
            return document.documentElement;
          };
          const sc = findScroller();
          const cards = document.querySelectorAll('#tab-tv .card');
          const last = cards[cards.length-1];
          const tabs = document.querySelector('nav.tabs').getBoundingClientRect();
          const lr = last ? last.getBoundingClientRect() : null;
          const lastMeta = last ? last.querySelector('.meta') : null;
          const mr = lastMeta ? lastMeta.getBoundingClientRect() : null;
          return {scroller: sc.tagName + '#' + sc.id,
                  scrolledTo: Math.round(sc.scrollTop), maxScroll: Math.round(sc.scrollHeight - sc.clientHeight),
                  lastCardBottom: lr ? Math.round(lr.bottom) : null,
                  lastMetaBottom: mr ? Math.round(mr.bottom) : null,
                  tabsTop: Math.round(tabs.top), tabsBottom: Math.round(tabs.bottom),
                  innerH: innerHeight,
                  metaHiddenByTabs: mr ? Math.max(0, Math.round(mr.bottom - tabs.top)) : null};
        }""")
        print("\n滚到底:", json.dumps(tail, ensure_ascii=False))

        await b.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
