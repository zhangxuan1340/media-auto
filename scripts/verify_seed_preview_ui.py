#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子预览图(缩略图 + 大图弹窗)前端回归

验证「种子预览图」三层能力:
  【渲染】① 后端给了 image 才渲染缩略图; 既没封面也没 infoHash 的行不留空位;
          ② 缩略图尺寸在桌面/移动两档都生效(移动端更小 —— 磁力行宽度是 A/B 调过的);
          ③ 缩略图不是链接、图片不可拖拽(移动端长按/拖动不会触发浏览器默认导航);
          ④ 追加行(「加载更多」)与首屏同口径: 金标/质量分徽章渲染一致;
          ⑤ 没封面但**有 infoHash** 的行渲染「预览」占位入口(否则这类行根本点不开,
             用户永远看不到内容截屏)。
  【弹窗】⑥ 点击缩略图/占位入口弹出大图层, 且**不发生任何页面跳转**;
          ⑦ 四种关闭方式: 点任意位置 / 右上角叉 / Esc / 移动端侧滑(后退);
          ⑧ ⚠️ 核心回归: 预览图打开时按后退(模拟 iOS 侧滑)**只关预览图**,
             不能把下面的详情弹窗一起关掉(用户反馈的"跳转"就是这么来的);
          ⑨ 种子里带引号/尖括号时做了转义(HTML 属性注入防护)。
  【内容预览】⑩ ⚠️ 核心: **列表渲染阶段一个 /api/magnet/preview 请求都不发**(点开才取),
             打开弹窗后才请求(打桩上游) —— 一次搜索几十行逐条预取会给每行加 1~1.7s;
          ⑪ 加载中显示 loading; 返回 5 张截图 → 渲染 16:9 网格、第一张选中,
             没封面时第一张截图顶上去当主图;
          ⑫ 点网格里某张 → 换主图 + 高亮转移 + **不关弹窗**;
          ⑬ 接口通但没截图 → 给明确提示(不是留一片空白)。

实现: 只起一个**静态文件服务**(python http.server, 指向 server/static),
不碰真实数据库 / 不起主服务 —— /api/* 全部 404, 页面停在登录页, 但 common.js /
detail.js 已经加载完毕, 直接调全局函数造 DOM 即可(与 8787 常驻解耦, 不会误写库)。
需要真 Chrome(channel="chrome") + service_workers="block"(SW 会缓存旧静态资源)。
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


def _px_ge(size, n):
    """'44x40' → 宽高都不小于 n 才为真(用来卡移动端触控目标尺寸)。"""
    try:
        w, h = (int(x) for x in str(size or "").split("x"))
    except Exception:  # noqa: BLE001
        return False
    return w >= n and h >= n


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
async (pixel) => {
  const out = {};
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const href0 = location.href;
  const state0 = JSON.stringify(history.state);
  const H_GOOD = 'a'.repeat(40);   // 打桩: 接口通, 给 5 张截图
  const H_BAD  = 'b'.repeat(40);   // 打桩: 接口通, 但没有截图

  // 统计 /api/magnet/preview 的请求次数 —— 用来证明「点开才取」(列表渲染阶段必须 0 次)
  const _origFetch = window.fetch.bind(window);
  window.__pvCalls = 0;
  window.fetch = (...a) => {
    if (String(a[0]).indexOf('/api/magnet/preview') >= 0) window.__pvCalls++;
    return _origFetch(...a);
  };

  // ---- ① 有图: 渲染缩略图 ----
  const box = document.createElement('div');
  box.id = 'probeBox';
  document.body.appendChild(box);
  box.innerHTML =
    '<div class="res">' + _seedThumb({image: pixel, name: '测试种子 A'}) +
    '<div class="info"><div class="n">名字</div></div></div>' +
    '<div class="res">' + _seedThumb({name: '既没封面也没 hash'}) +
    '<div class="info"><div class="n">名字2</div></div></div>' +
    '<div class="res res-compact">' + _seedThumb({image: pixel, name: '紧凑行'}) +
    '<div class="info"><div class="n">名字3</div></div></div>';
  const rows = box.querySelectorAll('.res');
  const th = rows[0].querySelector('.res-thumb');
  out.hasThumb = !!th;
  out.noThumbWhenNothing = rows[1].querySelector('.res-thumb') === null;   // 没封面也没 hash → 不留空位
  out.compactHasThumb = !!rows[2].querySelector('.res-thumb');
  if (th) {
    const cs = getComputedStyle(th);
    out.thumbSize = cs.width + 'x' + cs.height;
    out.thumbCursor = cs.cursor;
    out.thumbInline = th.closest('.res').firstElementChild === th;   // 缩略图在最左
    out.thumbNotInLink = !th.closest('a');                           // 不是链接 → 点了不会跳走
    const ti = th.querySelector('img');
    out.thumbImgNotDraggable = !!ti && ti.getAttribute('draggable') === 'false';
  }
  const cth = rows[2].querySelector('.res-thumb');
  out.compactSize = cth ? getComputedStyle(cth).width + 'x' + getComputedStyle(cth).height : null;

  // ---- ② 徽章口径: 追加行与首屏用同一套 helper ----
  out.goldBadge = _goldBadge({golden: true, name: 'x'}).indexOf('金标') >= 0;
  out.goldBadgeEmpty = _goldBadge({name: 'x'}) === '';
  out.qScoreOnlyInQualitySort = _qScoreTag({qualityScore: 88}, 'quality').indexOf('88') >= 0
    && _qScoreTag({qualityScore: 88}, 'relevance') === '';

  // ---- ③ 点缩略图: 打开弹窗, 且**不能发生跳转** ----
  th.click();
  await sleep(80);
  out.hrefUnchanged = location.href === href0;          // 没有导航
  out.href = location.href;
  out.histPushed = JSON.stringify(history.state) !== state0;   // 压了一层给侧滑用
  const bg = document.querySelector('.imgview-bg');
  out.popupOpened = !!bg;
  if (bg) {
    const bcs = getComputedStyle(bg);
    out.popupZ = bcs.zIndex;
    out.popupOverPrompt = (+bcs.zIndex) > 60;     // 要盖在输入/确认弹窗(60)之上
    out.popupUnderToast = (+bcs.zIndex) < 99;     // 但低于 toast(99)
    const imgEl = bg.querySelector('.imgview-main img');
    out.popupImgSrc = imgEl ? imgEl.getAttribute('src') : null;
    out.popupImgNotDraggable = !!imgEl && imgEl.getAttribute('draggable') === 'false';
    out.popupCaption = (bg.querySelector('.cap') || {}).textContent || '';
    out.noPvBlockWithoutHash = !bg.querySelector('.imgview-pv');   // 没 hash → 不显示内容预览区
    // 右上角叉按钮
    const xb = bg.querySelector('.imgview-x');
    out.hasCloseX = !!xb;
    if (xb) {
      const r = xb.getBoundingClientRect(), br = bg.getBoundingClientRect();
      out.xSize = Math.round(r.width) + 'x' + Math.round(r.height);
      out.xTopRight = (br.right - r.right) < 90 && (r.top - br.top) < 90;
      out.xHasLabel = !!xb.getAttribute('aria-label');
    }
    // 点任意处关闭
    bg.click();
    await sleep(300);
  }
  out.popupClosedByClick = !document.querySelector('.imgview-bg');
  out.histRestoredByClick = JSON.stringify(history.state) === state0;   // 自己压的那层退回去了

  // ---- ④ 打开 → 点叉关闭 ----
  th.click();
  await sleep(80);
  out.popupReopenedForX = !!document.querySelector('.imgview-bg');
  const xb2 = document.querySelector('.imgview-x');
  if (xb2) xb2.click();
  await sleep(300);
  out.popupClosedByX = !document.querySelector('.imgview-bg');

  // ---- ⑤ 打开 → Esc 关闭 ----
  th.click();
  await sleep(80);
  out.popupReopenedForEsc = !!document.querySelector('.imgview-bg');
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await sleep(300);
  out.popupClosedByEsc = !document.querySelector('.imgview-bg');

  // ---- ⑥ ⚠️ 核心: 预览图打开时后退(移动端侧滑)只关预览图, 不关详情弹窗 ----
  const mb = document.querySelector('#modalBg');
  mb.classList.add('show');
  th.click();
  await sleep(80);
  out.swipeOpened = !!document.querySelector('.imgview-bg');
  history.back();                                  // 模拟 iOS 侧滑 / 系统后退键
  await sleep(320);
  out.swipeClosedOnlyPreview = !document.querySelector('.imgview-bg')
    && mb.classList.contains('show');              // 详情弹窗还开着 = 没有"跳转"
  mb.classList.remove('show');

  // ---- ⑦ 无封面但有 infoHash → 占位入口; 内容预览**点开才取** ----
  box.innerHTML =
    '<div class="res">' + _seedThumb({infoHash: H_GOOD, name: '无封面·可预览'}) + '</div>' +
    '<div class="res">' + _seedThumb({infoHash: H_BAD, name: '无封面·无截图'}) + '</div>';
  const phList = box.querySelectorAll('.res-thumb-ph');
  const ph = phList[0];
  out.hasPlaceholderThumb = !!ph;
  out.twoPlaceholders = phList.length === 2;
  out.placeholderHasHash = !!ph && ph.dataset.hash === H_GOOD;
  out.placeholderLabel = !!ph && ph.textContent.indexOf('预览') >= 0;
  out.placeholderNotLink = !!ph && ph.tagName === 'DIV' && !ph.closest('a');
  out.noRequestBeforeOpen = window.__pvCalls === 0;    // ⚠️ 列表渲染阶段一次请求都不发
  ph.click();
  await sleep(120);
  out.phPopupOpened = !!document.querySelector('.imgview-bg');
  out.pvRequestedOnOpen = window.__pvCalls >= 1;       // 点开之后才发
  out.pvNoMainCover = !document.querySelector('.imgview-main img');   // 无封面 → 主图区先空着
  out.pvShowsLoading = !!document.querySelector('.imgview-bg .pv-body .spin');
  await sleep(350);                                    // 等打桩响应回来并渲染
  const pv = document.querySelector('.imgview-bg');
  const shots = pv ? pv.querySelectorAll('.imgview-shot') : [];
  out.shotCount = shots.length;
  out.firstShotOn = shots.length > 0 && shots[0].classList.contains('on');
  const mainImg = pv ? pv.querySelector('.imgview-main img') : null;
  out.mainFilledByFirstShot = !!mainImg && shots.length > 0
    && mainImg.getAttribute('src') === shots[0].getAttribute('data-src');
  const head = pv ? pv.querySelector('.pv-head') : null;
  out.pvHeadHasCount = !!head && head.textContent.indexOf('5') >= 0;
  if (shots.length >= 3) {
    shots[2].click();
    await sleep(90);
    const m2 = pv.querySelector('.imgview-main img');
    out.pickShotSwitchesMain = !!m2 && m2.getAttribute('src') === shots[2].getAttribute('data-src');
    out.pickShotMovesMark = shots[2].classList.contains('on') && !shots[0].classList.contains('on');
    out.pickShotKeepsOpen = !!document.querySelector('.imgview-bg');   // 点截图不关弹窗
  }
  if (pv && pv.querySelector('.imgview-x')) pv.querySelector('.imgview-x').click();
  await sleep(300);
  out.phPopupClosed = !document.querySelector('.imgview-bg');

  // ---- ⑧ 无截图 → 给明确提示(不是一片空白) ----
  phList[1].click();
  await sleep(400);
  const pv2 = document.querySelector('.imgview-bg');
  const emp = pv2 ? pv2.querySelector('.pv-empty') : null;
  out.emptyHintShown = !!emp;
  out.emptyHintText = emp ? emp.textContent : '';
  if (pv2 && pv2.querySelector('.imgview-x')) pv2.querySelector('.imgview-x').click();
  await sleep(300);
  out.totalPvCalls = window.__pvCalls;

  // ---- ⑨ 属性注入防护: 名字里带引号/尖括号不能逃出 data-name ----
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
    ['盗梦空间 (2010) 2160p UHD BluRay REMUX HDR10 x265 国语+中文字幕', pixel, 'c'.repeat(40)],
    ['Inception.2010.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265', pixel, ''],
    ['没封面的种子 → 占位入口, 点开看内容截屏', '', 'a'.repeat(40)],
  ];
  const box = document.createElement('div');
  box.id = 'demoBox';
  box.style.cssText = 'max-width:760px;margin:24px auto;padding:0 14px';
  box.innerHTML = '<h3 style="font-size:14px;margin:0 0 10px">种子预览图(缩略图 + 点开看内容预览)</h3>'
    + rows.map(([n, im, h]) => '<div class="res">' + _seedThumb({image: im, infoHash: h, name: n})
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
    good_shots = [f"https://whatslink.info/image/shot{i}" for i in range(1, 6)]

    async def stub_preview(route):
        """打桩 /api/magnet/preview: 'b'×40 的 hash → "接口通但没截图"; 其余 → 5 张截图。

        故意延迟 0.25s —— 真实上游(whatslink)要 1~1.7s, 不延迟的话响应瞬间就回来了,
        "加载中"那一帧根本抓不到, 断言会变成测时序而不是测行为。
        """
        await asyncio.sleep(0.25)
        if ("hash=" + "b" * 40) in route.request.url:
            await route.fulfill(json={"ok": False, "reason": "no_screenshots", "type": "UNKNOWN",
                                      "file_type": "", "size": 0, "count": 0, "name": "",
                                      "screenshots": []})
        else:
            await route.fulfill(json={"ok": True, "reason": "", "type": "FOLDER",
                                      "file_type": "folder", "size": 123456, "count": 2,
                                      "name": "demo", "screenshots": good_shots})
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
                await pg.route("**/api/magnet/preview**", stub_preview)
                await _load_page(pg, base)
                r = await pg.evaluate(PROBE, PIXEL)
                print(f"[{name}] {r}")
                check(f"[{name}] 有 image → 渲染缩略图且在最左", r["hasThumb"] and r.get("thumbInline"))
                check(f"[{name}] 没封面也没 infoHash → 不留空位", r.get("noThumbWhenNothing"))
                check(f"[{name}] 季扫描紧凑行也有缩略图", r["compactHasThumb"])
                check(f"[{name}] 缩略图尺寸 = {expect}",
                      r.get("thumbSize") == expect, r.get("thumbSize"))
                check(f"[{name}] 缩略图可点击(cursor: zoom-in)",
                      r.get("thumbCursor") == "zoom-in", r.get("thumbCursor"))
                check(f"[{name}] 缩略图不是链接 / 图片不可拖拽(移动端不触发默认导航)",
                      r.get("thumbNotInLink") and r.get("thumbImgNotDraggable"))
                check(f"[{name}] 金标徽章: 有 golden 才渲染",
                      r.get("goldBadge") and r.get("goldBadgeEmpty"))
                check(f"[{name}] 质量分徽章: 只在「质量优先」排序下渲染",
                      r.get("qScoreOnlyInQualitySort"))
                # --- 弹窗 ---
                check(f"[{name}] 点击缩略图弹出大图层", r["popupOpened"])
                check(f"[{name}] ⚠️ 打开预览图不发生页面跳转(location 不变)",
                      r.get("hrefUnchanged"), r.get("href"))
                check(f"[{name}] 打开预览图压了一层 history(供侧滑消费)", r.get("histPushed"))
                check(f"[{name}] 弹窗层级高于输入弹窗且低于 toast",
                      r.get("popupOverPrompt") and r.get("popupUnderToast"), r.get("popupZ"))
                check(f"[{name}] 弹窗里是同一张图, 且不可拖拽",
                      r.get("popupImgSrc") == PIXEL and r.get("popupImgNotDraggable"),
                      r.get("popupImgSrc"))
                check(f"[{name}] 弹窗带种子名说明", r.get("popupCaption") == "测试种子 A",
                      r.get("popupCaption"))
                check(f"[{name}] 没 infoHash 的行不显示内容预览区", r.get("noPvBlockWithoutHash"))
                check(f"[{name}] 有右上角叉按钮(带无障碍标签, 位置在右上)",
                      r.get("hasCloseX") and r.get("xTopRight") and r.get("xHasLabel"),
                      f'{r.get("xSize")}')
                check(f"[{name}] 叉按钮触控目标 ≥ 40px", _px_ge(r.get("xSize"), 40), r.get("xSize"))
                check(f"[{name}] 点任意处关闭", r["popupClosedByClick"])
                check(f"[{name}] 点任意处关闭后 history 状态复原",
                      r.get("histRestoredByClick"))
                check(f"[{name}] 再开 → 点叉关闭",
                      r["popupReopenedForX"] and r["popupClosedByX"])
                check(f"[{name}] 再开 → Esc 关闭",
                      r["popupReopenedForEsc"] and r["popupClosedByEsc"])
                check(f"[{name}] ⚠️ 后退/侧滑只关预览图, 不关详情弹窗(修\"跳转\")",
                      r.get("swipeOpened") and r.get("swipeClosedOnlyPreview"))
                # --- 无封面占位入口 + 内容预览「点开才取」 ---
                check(f"[{name}] 没封面但有 infoHash → 渲染占位入口(不是链接)",
                      r.get("hasPlaceholderThumb") and r.get("placeholderHasHash")
                      and r.get("placeholderNotLink") and r.get("placeholderLabel")
                      and r.get("twoPlaceholders"))
                check(f"[{name}] ⚠️ 列表渲染阶段不发内容预览请求(点开才取)",
                      r.get("noRequestBeforeOpen"))
                check(f"[{name}] 点占位入口 → 弹窗打开且此时才发请求",
                      r.get("phPopupOpened") and r.get("pvRequestedOnOpen"))
                check(f"[{name}] 加载中: 主图区为空 + 有 loading 指示",
                      r.get("pvNoMainCover") and r.get("pvShowsLoading"))
                check(f"[{name}] 内容预览渲染 5 张截图, 第一张选中",
                      r.get("shotCount") == 5 and r.get("firstShotOn"), r.get("shotCount"))
                check(f"[{name}] 无封面时用第一张截图顶主图",
                      r.get("mainFilledByFirstShot"))
                check(f"[{name}] 预览标题带张数", r.get("pvHeadHasCount"))
                check(f"[{name}] 点某张截图 → 换主图 + 高亮转移 + 不关弹窗",
                      r.get("pickShotSwitchesMain") and r.get("pickShotMovesMark")
                      and r.get("pickShotKeepsOpen"))
                check(f"[{name}] 关掉后再开另一次(共 2 次请求, 未被批量预取)",
                      r.get("phPopupClosed") and r.get("totalPvCalls") == 2,
                      r.get("totalPvCalls"))
                check(f"[{name}] 没有截图时给明确提示(不是空白)",
                      r.get("emptyHintShown") and "没有可用" in (r.get("emptyHintText") or ""),
                      r.get("emptyHintText"))
                check(f"[{name}] 种子名转义(属性注入防护)", r["injectionEscaped"])
                # 出效果图: ① 列表(缩略图/占位) ② 弹窗打开态(含内容预览)
                await pg.evaluate(DEMO, PIXEL)
                await pg.wait_for_timeout(250)
                await pg.screenshot(path=str(OUT / f"{name}_seed_thumb.png"))
                await pg.evaluate("document.querySelector('#demoBox .res-thumb').click()")
                await pg.wait_for_timeout(700)      # 等内容预览打桩响应渲染完
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
