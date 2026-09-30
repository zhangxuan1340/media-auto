#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【纯本地】通用设置页「保存配置」按钮状态回归(playwright + 系统 Chrome)

报障(2026-09-29): 在「管理 → 通用 → 种子抓取规则」里加了前排发布组, 灰条
「有未保存的配置变更」出来了, 但右边的「保存配置」是灰的点不动。

根因: cfgSave() 一进来就 `btn.disabled = true`, 只有失败分支复位; 成功分支走
_cfgLoad() 重绘表单, 而 cfgBar 是静态 HTML 不会被重绘 → 第一次保存之后按钮永远灰着,
下次编辑时灰条带着一个点不动的按钮出现。

修法: _cfgTouch() 里"只要又有编辑就放行"(再存一次没问题), cfgSave 成功后也复位。

本脚本起自己的服务: 临时库(MEDIA_AUTO_DB) + 独立随机端口, 不碰真实 data/media_auto.db。
用法: ./venv/bin/python scripts/verify_settings_save.py
退出码: 0 = 全部通过; 1 = 有失败项 / 服务起不来 / 没浏览器
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def click_save(btn, label):
    """点保存; 按钮被置灰时 playwright 会一直等 enabled → 这里限时, 让失败变成
    干净的 FAIL 而不是卡 30 秒(原 bug 就是按钮永久灰, 点不动)。"""
    try:
        await btn.click(timeout=6000)
        return True
    except Exception as e:  # noqa: BLE001
        check(label, False, str(e).splitlines()[0][:160])
        return False


async def flow(base):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = None
        for launch_kw in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(**launch_kw)
                print(f"  浏览器: {'系统 Chrome' if launch_kw else '自带 chromium'}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ 启动失败({e.__class__.__name__}), 回退")
        if browser is None:
            check("有可用浏览器", False, "系统 Chrome 与自带 chromium 都起不来")
            return

        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))

        # 登录
        await page.goto(base, wait_until="networkidle")
        if await page.locator("#login").is_visible():
            await page.fill("#lu", "admin")
            await page.fill("#lp", "verify_pass_123")
            await page.locator("#loginForm button[type=submit]").click()
            await page.wait_for_timeout(1200)
        check("登录进控制台", not await page.locator("#login").is_visible())

        # 管理 → 通用 → 种子抓取规则页签
        await page.evaluate("switchTab('manage')")
        await page.wait_for_timeout(500)
        await page.evaluate("switchManageSub('general')")
        await page.wait_for_selector("#cfgTabs", timeout=20000)
        await page.locator("#cfgTabs button", has_text="种子抓取规则").click()
        await page.wait_for_timeout(300)

        ta = page.locator('#manageBody [data-cfg="search.group_priority"]')
        golden = page.locator('#manageBody [data-cfg="search.golden_groups"]')
        check("前排发布组输入框在页面上", await ta.count() == 1)
        check("金标自压组输入框在页面上", await golden.count() == 1)

        bar = page.locator("#cfgBar")
        btn = page.locator("#cfgSaveBtn")

        # 1) 第一次编辑 → 灰条出现 + 保存按钮可点
        await ta.fill("FRDS\nBeitai")
        await golden.fill("FRDS")
        check("编辑后出现「有未保存」灰条", await bar.is_visible())
        check("第一次编辑后保存按钮可点", await btn.is_enabled())

        # 2) 保存成功(成功路径原本会把按钮永久置灰)
        if await click_save(btn, "第一次保存点得动"):
            await page.wait_for_timeout(2500)
            toast = (await page.locator("#toast").text_content() or "")
            check("第一次保存成功", "保存" in toast, toast)

        # 3) 回归点: 再编辑一次, 按钮必须还/又能点(原 bug: 灰的点不动)
        await ta.fill("FRDS\nBeitai\nHHD")
        await golden.fill("FRDS\nBeitai")
        check("第二次编辑后灰条再次出现", await bar.is_visible())
        check("第二次编辑后保存按钮仍可点(原 bug: 永久灰)", await btn.is_enabled())

        if await click_save(btn, "第二次保存点得动"):
            await page.wait_for_timeout(2500)
            toast2 = (await page.locator("#toast").text_content() or "")
            check("第二次保存成功", "保存" in toast2, toast2)

        # 4) 落库回读(整份 PUT → GET 往返, 组名一个不少)
        cfg = await page.evaluate(
            "fetch('/api/config',{credentials:'same-origin'}).then(r=>r.json())")
        sec = ((cfg.get("config") or {}).get("search") or {})
        check("前排发布组两轮都落库", sec.get("group_priority") == ["FRDS", "Beitai", "HHD"],
              sec.get("group_priority"))
        check("金标组两轮都落库", sec.get("golden_groups") == ["FRDS", "Beitai"],
              sec.get("golden_groups"))

        check("页面无 JS 运行时错误", not errs, errs[:3])
        await ctx.close()
        await browser.close()


def main():
    tmp = tempfile.mkdtemp(prefix="media_auto_cfgsave_")
    db = os.path.join(tmp, "verify.db")
    os.environ["MEDIA_AUTO_DB"] = db
    port = free_port()

    # 播种默认配置 → 改端口/账号 → 标记引导完成(服务起来就能直接登录)
    from lib import config as lc          # noqa: PLC0415
    cfg = lc.load_config()
    cfg.setdefault("web", {})
    cfg["web"]["host"] = "127.0.0.1"
    cfg["web"]["port"] = port
    cfg["web"]["auth"] = {"username": "admin", "password": "verify_pass_123"}
    lc.save_config(cfg)
    lc.set_setup_done(True)

    env = dict(os.environ, MEDIA_AUTO_DB=db)
    logf = open(os.path.join(tmp, "server.log"), "w")   # noqa: SIM115 失败时好排查
    srv = subprocess.Popen([sys.executable, "-m", "server.main"], cwd=str(ROOT),
                           env=env, stdout=logf, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        ready = False
        for _ in range(80):
            if srv.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/", timeout=2) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        check("服务起来了", ready, f"见 {tmp}/server.log")
        if ready:
            import asyncio                    # noqa: PLC0415
            asyncio.run(flow(base))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:  # noqa: BLE001
            srv.kill()
        logf.close()

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        print(f"  服务日志: {tmp}/server.log")
        return 1
    print("  全部通过")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
