#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【纯本地】管理 → 分类规则 → 地区档 UI 回归(playwright + 系统 Chrome)

需求(2026-09-30): 分类的地区要能手动选择 —— 归属可改、可新增地区档(如单独 Tw 档)。

本脚本走真实页面:
  1) 地区档表渲染 6 档, 兜底档 Ot 不可删
  2) 新增档 → 改键 Tw / 填档名/国家/优先 → 灰条出现 → 保存 → 落库 + 行随档变
     (目录映射的「按国家/地区」从 12 行变 14 行, 出现 TwMovie/TwShow)
  3) 键改成特殊类型名 Dm → 输入框标红(前端先拦)
  4) 给 TwMovie 改目录名 → 保存 → 回读
  5) 删档(弹确认) → 保存 → 行数回到 6, TwMovie 目录名一并清掉
  全程用临时库(MEDIA_AUTO_DB) + 随机端口, 不碰真实 data/media_auto.db。

用法: ./venv/bin/python scripts/verify_region_rules_ui.py
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


async def click(btn, label, timeout=6000):
    try:
        await btn.click(timeout=timeout)
        return True
    except Exception as e:  # noqa: BLE001
        check(label, False, str(e).splitlines()[0][:160])
        return False


def region_rows(page):
    return page.locator(".rg-table tbody tr")


async def fill(page, key, field, value):
    sel = f'.rg-table tr[data-k="{key}"] input[data-f="{field}"]'
    await page.locator(sel).fill(value)
    await page.wait_for_timeout(120)


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

        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000})
        page = await ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        # 删档弹的是 confirm() —— 一律同意(顺带把文案记下来)
        dialogs = []
        async def _on_dialog(d):
            dialogs.append(d.message)
            await d.accept()
        page.on("dialog", _on_dialog)

        # 登录
        await page.goto(base, wait_until="networkidle")
        if await page.locator("#login").is_visible():
            await page.fill("#lu", "admin")
            await page.fill("#lp", "verify_pass_123")
            await page.locator("#loginForm button[type=submit]").click()
            await page.wait_for_timeout(1200)
        check("登录进控制台", not await page.locator("#login").is_visible())

        # 管理 → 分类规则
        await page.evaluate("switchTab('manage')")
        await page.wait_for_timeout(400)
        await page.evaluate("switchManageSub('category')")
        await page.wait_for_selector(".rg-table tbody tr", timeout=20000)
        await page.wait_for_timeout(400)

        # 1) 初始渲染
        rows = region_rows(page)
        n0 = await rows.count()
        check("1) 地区档表默认 6 档", n0 == 6, n0)
        labels = await page.locator(".rg-table tbody tr input[data-f='label']").evaluate_all(
            "els => els.map(e => e.value)")
        check("1) 档名是中文标签", labels[:3] == ["中国大陆", "欧美", "日韩"], labels)
        keys = await page.locator(".rg-table tbody tr").evaluate_all(
            "els => els.map(e => e.dataset.k)")
        check("1) 兜底档 Ot 在最后", keys[-1] == "Ot", keys)
        check("1) Ot 行的删除按钮不可点",
              await page.locator('.rg-table tr[data-k="Ot"] .rg-ops button').last.is_disabled())
        sub = await page.locator(".rg-sub").first.text_content()
        check("1) 标题显示档数与生成键数", "6 档" in (sub or "") and "12" in (sub or ""), sub)
        n_region_rows = await page.locator(".cat-group").count()
        check("1) 目录映射仍有两组", n_region_rows >= 2, n_region_rows)
        region_group = await page.locator("#manageBody table").last.locator("tbody tr").count()
        # 最后一张表 = 「按国家/地区」组: 12 个分类行 + 1 个组标题行
        check("1) 地区目录行 = 12(6 档 × 2) + 组标题", region_group == 13, region_group)

        # 2) 新增档 → 改键 Tw → 保存
        await page.locator(".rg-add button", has_text="新增地区档").click()
        await page.wait_for_timeout(300)
        check("2) 新增后 7 档", await region_rows(page).count() == 7)
        check("2) 灰条出现", await page.locator("#catBar").is_visible())
        new_key = await page.locator(".rg-table tbody tr").last.get_attribute("data-k")
        await fill(page, new_key, "_key", "Tw")
        await fill(page, "Tw", "label", "台湾")
        await fill(page, "Tw", "display", "台片")
        await fill(page, "Tw", "countries", "TW")
        await fill(page, "Tw", "prio", "TW=zh/cn")
        await fill(page, "Tw", "keywords", "台片, 台剧")
        # 同一个国家只能属于一个档 → 先把 TW 从港台档的归属里摘掉(这就是「归属可改」)
        await fill(page, "Hk", "countries", "HK")
        await fill(page, "Hk", "prio", "HK")
        barmsg = await page.locator(".cat-barmsg").text_content()
        check("2) 灰条提示含地区档", "地区档" in (barmsg or ""), barmsg)

        btn = page.locator("#catSaveBtn")
        if await click(btn, "2) 保存点得动"):
            await page.wait_for_timeout(2500)
            toast = await page.locator("#toast").text_content() or ""
            check("2) 保存成功且提示档数", "地区档已更新" in toast and "7 档" in toast, toast)

        # 落库回读
        cfg = await page.evaluate(
            "fetch('/api/config',{credentials:'same-origin'}).then(r=>r.json())")
        regions = ((cfg.get("config") or {}).get("regions") or {})
        check("2) regions 落库且含 Tw", "Tw" in (regions.get("order") or []), regions.get("order"))
        check("2) Tw 的国家/优先落库",
              regions.get("items", {}).get("Tw", {}).get("countries") == ["TW"]
              and regions.get("items", {}).get("Tw", {}).get("prio") == {"TW": ["zh", "cn"]},
              regions.get("items", {}).get("Tw"))

        # 3) 保存后重绘: 7 档、键只读、目录映射多出 TwMovie/TwShow
        await page.wait_for_selector(".rg-table tbody tr", timeout=15000)
        check("3) 保存后仍是 7 档", await region_rows(page).count() == 7)
        check("3) 保存后键变只读",
              await page.locator('.rg-table tr[data-k="Tw"] input[data-f="_key"]').get_attribute(
                  "readonly") is not None)
        tw_keys = await page.locator(".cat-name small").all_text_contents()
        check("3) 目录映射出现 TwMovie/TwShow", "TwMovie" in tw_keys and "TwShow" in tw_keys,
              tw_keys)

        # 4) 前端先拦非法键(撞特殊类型) —— 只在新增的档上可改, 这里先把 Tw 临时改回可编辑
        #    (键已只读 → 改用新增第二档来验)
        await page.locator(".rg-add button", has_text="新增地区档").click()
        await page.wait_for_timeout(250)
        k2 = await page.locator(".rg-table tbody tr").last.get_attribute("data-k")
        await fill(page, k2, "_key", "Dm")
        cls = await page.locator('.rg-table tbody tr').last.locator(
            'input[data-f="_key"]').get_attribute("class")
        check("4) 撞特殊类型的键标红", "rg-bad" in (cls or ""), cls)
        await page.locator('.rg-table tbody tr').last.locator('.rg-ops button').nth(2).click()
        await page.wait_for_timeout(300)
        check("4) 撤掉刚加的档(未保存, 不落库)", await region_rows(page).count() == 7)

        # 5) 给 TwMovie 改目录名 → 保存 → 回读
        tw_in = page.locator('#manageBody input[data-key="TwMovie"]')
        check("5) TwMovie 目录名输入框在", await tw_in.count() == 1)
        await tw_in.fill("台湾电影")
        await page.wait_for_timeout(200)
        if await click(page.locator("#catSaveBtn"), "5) 保存目录名点得动"):
            await page.wait_for_timeout(2500)
            toast2 = await page.locator("#toast").text_content() or ""
            check("5) 目录名保存成功", "保存" in toast2, toast2)
        cfg2 = await page.evaluate(
            "fetch('/api/config',{credentials:'same-origin'}).then(r=>r.json())")
        cats = (cfg2.get("config") or {}).get("categories") or {}
        check("5) TwMovie 目录名落库", cats.get("TwMovie") == "台湾电影", cats.get("TwMovie"))
        check("5) 没有把老目录名改坏", cats.get("CnMovie") in (None, "CnMovie"), cats.get("CnMovie"))

        # 6) 删档(弹 confirm, 上面已挂统一同意的 handler)→ 保存 → 行数回 6, 目录名一并清掉
        await page.locator('.rg-table tr[data-k="Tw"] .rg-ops button').last.click()
        await page.wait_for_timeout(400)
        check("6) 删档后剩 6 档", await region_rows(page).count() == 6)
        if await click(page.locator("#catSaveBtn"), "6) 删除后保存点得动"):
            await page.wait_for_timeout(2500)
            toast3 = await page.locator("#toast").text_content() or ""
            check("6) 删除保存成功", "保存" in toast3, toast3)
        cfg3 = await page.evaluate(
            "fetch('/api/config',{credentials:'same-origin'}).then(r=>r.json())")
        regions3 = (cfg3.get("config") or {}).get("regions") or {}
        cats3 = (cfg3.get("config") or {}).get("categories") or {}
        check("6) Tw 档从落库配置里消失", "Tw" not in (regions3.get("order") or []),
              regions3.get("order"))
        check("6) TwMovie 目录名被清掉", "TwMovie" not in cats3, cats3)
        await page.wait_for_selector(".rg-table tbody tr", timeout=15000)
        check("6) 页面回到 6 档", await region_rows(page).count() == 6)

        check("页面无 JS 运行时错误", not errs, errs[:3])
        await ctx.close()
        await browser.close()


def main():
    tmp = tempfile.mkdtemp(prefix="media_auto_regionui_")
    db = os.path.join(tmp, "verify.db")
    os.environ["MEDIA_AUTO_DB"] = db
    port = free_port()

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
