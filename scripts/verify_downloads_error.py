#!/usr/bin/env python3
"""「管理 → 下载」页错误态与自愈能力验证(故障注入)

为什么要单独测:
  qBittorrent 不可达/认证失败时, 下载页必须 ① 把错误显示出来 ② 提供重试入口
  ③ **继续后台重试**, 而不是永久停在错误上。
  2026-09-21 用户报的 "读取 qBittorrent 任务失败: qBittorrent 登录失败: HTTP 204"
  就是 ③ 缺失: 失败分支里 data.ok=false → torrents=[] → active=false → 停掉轮询,
  结果错误横幅一直在, 只能手动切页签才能恢复。

⚠️ 测试坑: **page.route 拦不到 Service Worker 发起的请求**。本项目注册了 SW 接管
   /api/*, 所以必须先 `service_workers="block"` 关掉 SW, 否则故障注入不生效
   (请求照常打到真服务端并成功, 测出来的"没问题"是假的)。

用法: env -u HTTP_PROXY -u HTTPS_PROXY venv/bin/python scripts/verify_downloads_error.py
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"
OUT = ROOT / "state" / "ui_verify" / "pwa_safearea"
MSG = "读取 qBittorrent 任务失败: qBittorrent 登录失败: HTTP 204"


async def main():
    from playwright.async_api import async_playwright
    a = json.loads((ROOT / "config.json").read_text())["web"]["auth"]
    OUT.mkdir(parents=True, exist_ok=True)
    bad = []

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome")
        ctx = await b.new_context(viewport={"width": 393, "height": 852},
                                  device_scale_factor=3, is_mobile=True, has_touch=True,
                                  service_workers="block")
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await ctx.add_init_script("try{localStorage.setItem('media_theme','light')}catch(e){}")
        await pg.goto(BASE, wait_until="networkidle")
        if await pg.locator("#login").is_visible():
            await pg.fill("#lu", a["username"])
            await pg.fill("#lp", a["password"])
            await pg.locator("#loginForm button[type=submit]").click()
            await pg.wait_for_timeout(900)

        fail = {"on": True}

        async def handler(route):
            if fail["on"] and "/api/qbit/torrents" in route.request.url:
                await route.fulfill(status=502, content_type="application/json",
                                    body=json.dumps({"detail": MSG}, ensure_ascii=False))
            else:
                await route.continue_()

        await pg.route("**/api/qbit/**", handler)
        await pg.evaluate("switchTab('manage')")
        await pg.wait_for_timeout(400)
        await pg.evaluate("switchManageSub('download')")
        await pg.wait_for_timeout(2500)
        await pg.evaluate("() => {document.documentElement.style.setProperty('--sat','59px');"
                          "document.documentElement.style.setProperty('--sab','34px');}")
        await pg.wait_for_timeout(200)

        # ---- 1. 故障态: 横幅可见(首屏) + 有重试按钮 + 轮询仍在跑 ----
        d = await pg.evaluate("""() => {
          const w = document.querySelector('#manageBody .dl-warn');
          const r = w ? w.getBoundingClientRect() : null;
          return {
            warn: w ? w.innerText.replace(/\\s+/g,' ').trim() : null,
            hasBtn: w ? !!w.querySelector('button') : false,
            inFirstScreen: r ? (r.top >= 0 && r.top < innerHeight) : false,
            timerAlive: (typeof _dlTimer !== 'undefined') && _dlTimer !== null,
          };
        }""")
        print("1. 故障态:", json.dumps(d, ensure_ascii=False))
        if not d["warn"]:
            bad.append("故障时没有显示错误横幅")
        elif MSG not in d["warn"]:
            bad.append(f"错误文案不对: {d['warn']}")
        if not d["hasBtn"]:
            bad.append("错误横幅里没有「重试」按钮")
        if not d["inFirstScreen"]:
            bad.append("错误横幅不在首屏(被配置表单挤到下面了)")
        if not d["timerAlive"]:
            bad.append("出错后轮询停了 —— 错误会永久停留在页面上")
        await pg.screenshot(path=str(OUT / "dl_error_state.png"))

        # ---- 2. 点「重试」立即恢复 ----
        fail["on"] = False
        await pg.evaluate("() => document.querySelector('#manageBody .dl-warn button').click()")
        await pg.wait_for_timeout(3000)
        r = await pg.evaluate("""() => ({
          warnGone: !document.querySelector('#manageBody .dl-warn'),
          text: document.querySelector('#manageBody').innerText.replace(/\\s+/g,' ').slice(0, 60),
        })""")
        print("2. 点重试后:", json.dumps(r, ensure_ascii=False))
        if not r["warnGone"]:
            bad.append("点「重试」后没有恢复")
        await pg.screenshot(path=str(OUT / "dl_recovered.png"))

        # ---- 3. 不点按钮, 靠后台自动重试自愈 ----
        fail["on"] = True
        await pg.evaluate("loadDownloads()")
        await pg.wait_for_timeout(1500)
        if not await pg.evaluate("() => !!document.querySelector('#manageBody .dl-warn')"):
            bad.append("注入故障后未出现横幅")
        fail["on"] = False
        await pg.wait_for_timeout(18000)      # 慢速重试间隔 15s, 留余量
        auto = await pg.evaluate("() => !document.querySelector('#manageBody .dl-warn')")
        print("3. 纯自动重试(18s 后自愈):", auto)
        if not auto:
            bad.append("网络恢复后错误没有自动消失(缺少后台重试)")

        if errs:
            bad.append(f"JS 错误: {errs}")
        print("   JS 错误:", errs or "无")
        await b.close()

    print("\n" + "=" * 60)
    if bad:
        print("FAIL:")
        for x in bad:
            print("  -", x)
        return 1
    print("PASS: 错误可见(首屏)+有重试按钮+后台自动重试自愈")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
