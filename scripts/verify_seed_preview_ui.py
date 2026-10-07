#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子预览图(缩略图 + 大图弹窗)前端回归

验证 2026-10-07 新增的「种子预览图」:
  ① 后端给了 image 才渲染缩略图; 没图的行不留空位;
  ② 缩略图尺寸在桌面/移动两档都生效(移动端更小 —— 磁力行宽度是 A/B 调过的);
  ③ 点击缩略图弹出大图层(.imgview-bg), 点任意处 / Esc 都能关掉;
  ④ 种子名里的引号/尖括号做了转义(HTML 属性注入防护);
  ⑤ 程序化生成的 img 用 src 里的 img() 走图片缓存代理(TMDB 地址时)。

实现: 只起一个**静态文件服务**(python -m http.server, 指向 server/static),
不碰真实数据库 / 不起主服务 —— /api/* 全部 404, 页面停在登录页, 但 common.js /
detail.js 已经加载完毕, 直接调全局函数造 DOM 即可(与 8787 常驻解耦, 不会误写库)。
用法: venv/bin/python scripts/verify_seed_preview_ui.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import functools
import http.server
import os
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STATIC = ROOT / "server" / "static"
OUT = ROOT / "state" / "ui_verify"

FAILED = []
# 1x1 透明 PNG(data URL 不会被 img() 改写, 也不依赖网络 → 断言稳定)
PIXEL = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
         "z8AAAwAB/AF+8h0AAAAASUVORK5CYII=")


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # 静音访问日志
        pass


def start_static_server():
    port = _free_port()
    handler = functools.partial(_Quiet, directory=str(STATIC))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# 在页面里造行并断言 —— 全部走真实 DOM + 真实 CSS
PROBE = r"""
(pixel) => {
  const out = {};
  // ---- ① 有图: 渲染缩略图 ----
  const box = document.createElement('div');
  box.id = 'probeBox';
  document.body.appendChild(box);
  box.innerHTML =
    '<div class="res">' + _seedThumb({image: pixel, name: '测试种子 A'}) +
    '<div class="info"><div class="n">名字</div></div></div>' +
    '<div class="res">' + _seedThumb({name: '没有图的种子'}) +
    '<div class="info"><div class="n">名字2</div></div></div>' +
    '<div class="res res-compact">' + _seedThumb({image: pixel, name: '紧凑行'}) +
    '<div class="info"><div class="n">名字3</div></div></div>';
  const rows = box.querySelectorAll('.res');
  const th = rows[0].querySelector('.res-thumb');
  out.hasThumb = !!th;
  out.noThumbWhenNoImage = rows[1].querySelector('.res-thumb') === null;
  out.compactHasThumb = !!rows[2].querySelector('.res-thumb');
  if (th) {
    const cs = getComputedStyle(th);
    out.thumbSize = cs.width + 'x' + cs.height;
    out.thumbCursor = cs.cursor;
    out.thumbInline = th.closest('.res').firstElementChild === th;   // 缩略图在最左
  }
  const cth = rows[2].querySelector('.res-thumb');
  out.compactSize = cth ? getComputedStyle(cth).width + 'x' + getComputedStyle(cth).height : null;

  // ---- ② 弹窗: 点开 → 断言 → 点背景关 ----
  th.click();
  const bg = document.querySelector('.imgview-bg');
  out.popupOpened = !!bg;
  if (bg) {
    const bcs = getComputedStyle(bg);
    out.popupZ = bcs.zIndex;
    out.popupCursor = bcs.cursor;
    const imgEl = bg.querySelector('img');
    out.popupImgSrc = imgEl ? imgEl.getAttribute('src') : null;
    out.popupCaption = (bg.querySelector('.cap') || {}).textContent || '';
    out.popupOverPrompt = (+bcs.zIndex) > 60;     // 要盖在输入/确认弹窗(60)之上
    out.popupUnderToast = (+bcs.zIndex) < 99;     // 但低于 toast(99)
    bg.click();                                   // 点任意处关闭
  }
  out.popupClosedByClick = !document.querySelector('.imgview-bg');

  // ---- ③ Esc 关闭 ----
  th.click();
  out.popupReopened = !!document.querySelector('.imgview-bg');
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  out.popupClosedByEsc = !document.querySelector('.imgview-bg');

  // ---- ④ 属性注入防护: 名字里带引号/尖括号不能逃出 data-name ----
  box.innerHTML = '<div class="res">' +
    _seedThumb({image: pixel, name: 'a"><img src=x onerror=alert(1)>'}) + '</div>';
  const injTh = box.querySelector('.res-thumb');
  out.injectionEscaped = !!injTh && injTh.dataset.name.indexOf('onerror') >= 0
    && box.querySelectorAll('img').length === 1;   // 只有缩略图那一张, 没被注入第二张
  box.remove();
  return out;
}
"""


async def _load_page(pg, base):
    """打开静态页并等到 detail.js 的全局函数就绪。

    偶尔静态资源会取不齐(14 个 script 并发), 这时 detail.js 没执行 → 函数不存在;
    重试一次即可, 避免把一次抓取抖动当成门禁失败。
    """
    last = None
    for _ in range(3):
        await pg.goto(base, wait_until="load")
        try:
            await pg.wait_for_function(
                "() => typeof _seedThumb === 'function'"
                " && typeof previewSeedImage === 'function'", timeout=15000)
            return
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


# 造一组"像真的一样"的磁力行, 用于出效果截图(不含断言)
DEMO = r"""
(pixel) => {
  const rows = [
    ['盗梦空间 (2010) 2160p UHD BluRay REMUX HDR10 x265 国语+中文字幕', pixel],
    ['Inception.2010.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265', pixel],
    ['没有封面的种子(Next-Web 站的 REST 接口不给图) — 不渲染缩略图', ''],
    ['树屋大师 第一季 S01E03 1080p WEB-DL H.264 AAC', pixel],
  ];
  const box = document.createElement('div');
  box.id = 'demoBox';
  box.style.cssText = 'max-width:760px;margin:24px auto;padding:0 14px';
  box.innerHTML = '<h3 style="font-size:14px;margin:0 0 10px">种子预览图(缩略图 + 点击弹窗)</h3>'
    + rows.map(([n, im]) => '<div class="res">' + _seedThumb({image: im, name: n})
      + '<div class="info"><div class="n">' + n + '</div>'
      + '<div class="s"><span class="sz">12.34 GB</span></div></div>'
      + '<div class="res-btns"><button class="push">推送 CD2</button>'
      + '<button class="push qbit">推送 Qbit</button></div></div>').join('');
  document.body.appendChild(box);
  return box.querySelectorAll('.res-thumb').length;
}
"""


async def main():
    from playwright.async_api import async_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    srv, port = start_static_server()
    base = f"http://127.0.0.1:{port}"
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(channel="chrome", headless=True)
            for name, vp, expect in (("desktop", {"width": 1440, "height": 900}, "44pxx66px"),
                                     ("mobile", {"width": 393, "height": 852,
                                                 "is_mobile": True, "dsf": 2}, "38pxx57px")):
                ctx = await b.new_context(
                    viewport={"width": vp["width"], "height": vp["height"]},
                    is_mobile=vp.get("is_mobile", False),
                    device_scale_factor=vp.get("dsf", 1),
                    service_workers="block")     # SW 会缓存旧静态资源, 门禁一律禁掉
                pg = await ctx.new_page()
                await _load_page(pg, base)
                r = await pg.evaluate(PROBE, PIXEL)
                print(f"[{name}] {r}")
                check(f"[{name}] 有 image → 渲染缩略图且在最左", r["hasThumb"] and r.get("thumbInline"))
                check(f"[{name}] 无 image → 不留空位", r["noThumbWhenNoImage"])
                check(f"[{name}] 季扫描紧凑行也有缩略图", r["compactHasThumb"])
                check(f"[{name}] 缩略图尺寸 = {expect}",
                      r.get("thumbSize") == expect, r.get("thumbSize"))
                check(f"[{name}] 缩略图可点击(cursor: zoom-in)",
                      r.get("thumbCursor") == "zoom-in", r.get("thumbCursor"))
                check(f"[{name}] 点击缩略图弹出大图层", r["popupOpened"])
                check(f"[{name}] 弹窗层级高于输入弹窗且低于 toast",
                      r.get("popupOverPrompt") and r.get("popupUnderToast"), r.get("popupZ"))
                check(f"[{name}] 弹窗里是同一张图", r.get("popupImgSrc") == PIXEL, r.get("popupImgSrc"))
                check(f"[{name}] 弹窗带种子名说明", r.get("popupCaption") == "测试种子 A",
                      r.get("popupCaption"))
                check(f"[{name}] 点任意处关闭", r["popupClosedByClick"])
                check(f"[{name}] 再开 → Esc 关闭", r["popupReopened"] and r["popupClosedByEsc"])
                check(f"[{name}] 种子名转义(属性注入防护)", r["injectionEscaped"])
                # 出效果图: ① 列表(缩略图) ② 弹窗打开态
                await pg.evaluate(DEMO, PIXEL)
                await pg.wait_for_timeout(250)
                await pg.screenshot(path=str(OUT / f"{name}_seed_thumb.png"))
                await pg.evaluate("document.querySelector('#demoBox .res-thumb').click()")
                await pg.wait_for_timeout(250)
                await pg.screenshot(path=str(OUT / f"{name}_seed_preview.png"))
                await pg.evaluate("document.querySelector('#demoBox').remove()")
                await ctx.close()
            await b.close()
    finally:
        srv.shutdown()

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
