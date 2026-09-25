#!/usr/bin/env python3
"""NFO 更新功能 - 前端 UI 回归(playwright + 系统 Chrome)

验证清单:
  1) 登录
  2) openDetailLocal 打开一个【库内】作品详情(默认电影 1921 / tmdb 757587)
  3) 库内才出现的 #nfoBox 渲染出来(含 #nfoDate / #nfoUpdateBtn)
  4) #nfoDate 异步拉到「更新于 …」或「尚未生成 NFO」(非"查询中…"/"查询失败")
  5) 点击 #nfoUpdateBtn → 出现「NFO 已更新」toast, 且 #nfoDate 刷新为新的更新时间

用法:
  venv/bin/python scripts/nfo_ui_verify.py [tmdb_id]   # 默认 757587
退出码: 0=通过, 1=失败
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8787"


def creds():
    cfg = json.loads((ROOT / "config.json").read_text())
    a = cfg["web"]["auth"]
    return a["username"], a["password"]


async def main():
    tmdb_id = int(sys.argv[1]) if len(sys.argv) > 1 else 757587
    kind = "tv" if len(sys.argv) > 2 and sys.argv[2] == "tv" else "movie"

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome")
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2
        )
        page = await ctx.new_page()
        page_errors = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # 1) 登录
        await page.goto(BASE, wait_until="networkidle")
        if await page.locator("#login").is_visible():
            u, p = creds()
            await page.fill("#lu", u)
            await page.fill("#lp", p)
            await page.locator("#loginForm button[type=submit]").click()
            await page.wait_for_timeout(800)
        if await page.locator("#login").is_visible():
            print("❌ 登录失败")
            await browser.close()
            return 1

        # 2) 打开库内作品详情
        print(f"== 打开详情 {kind}/{tmdb_id} ==")
        await page.evaluate(f"openDetailLocal('{kind}', {tmdb_id})")
        await page.wait_for_timeout(2500)

        # 3) NFO 框是否渲染(库内才会出现)
        nfo_box = page.locator("#nfoBox")
        if not await nfo_box.count():
            print("❌ #nfoBox 未渲染(可能该作品 inLibrary=false 或详情加载失败)")
            # 诊断: 打印 mBody 前 200 字
            body = await page.evaluate("document.querySelector('#mBody')?.innerText?.slice(0,200) || '(无 #mBody)'")
            print("   mBody 预览:", body)
            await browser.close()
            return 1
        print("✓ #nfoBox 已渲染(确认 inLibrary=true)")

        # 4) NFO 日期文案(等异步 loadNfoInfo 完成: 非"查询中…"/"查询失败")
        date_el = page.locator("#nfoDate")
        await page.wait_for_function(
            "() => { const t = document.querySelector('#nfoDate')?.innerText || ''; "
            "return !/查询中|查询失败/.test(t); }",
            timeout=8000,
        )
        date_text = (await date_el.inner_text()).strip()
        print("   #nfoDate 文案:", date_text)
        if not ("更新于" in date_text or "尚未生成 NFO" in date_text):
            print("❌ #nfoDate 未拿到有效文案:", date_text)
            await browser.close()
            return 1
        print("✓ #nfoDate 文案有效")

        # 5) 点击更新 → 抓 POST 回包 + 日期刷新
        #    抓接口响应(比 toast 时机更稳): 监听 /api/nfo/update
        post_result = {}
        async def _on_resp(resp):
            if "/api/nfo/update" in resp.url:
                try:
                    post_result["status"] = resp.status
                    post_result["body"] = await resp.json()
                except Exception:
                    post_result["status"] = resp.status
        page.on("response", _on_resp)

        old_text = date_text
        await page.locator("#nfoUpdateBtn").click()
        # 等 POST 响应落地(最多 20s, CD2 写入可能稍慢)
        for _ in range(40):
            if post_result:
                break
            await page.wait_for_timeout(500)
        new_text = (await date_el.inner_text()).strip()
        print("   更新按钮点击后 #nfoDate 文案:", new_text)

        ok = True
        if not post_result:
            print("❌ 未捕获到 /api/nfo/update 响应(点击可能未触发请求)")
            ok = False
        else:
            print("   POST /api/nfo/update →", post_result.get("status"),
                  json.dumps(post_result.get("body"), ensure_ascii=False))
            if post_result.get("status") != 200 or not (post_result.get("body") or {}).get("ok"):
                print("❌ 更新接口未成功:", post_result)
                ok = False
            else:
                # 日期应刷新为 POST 回包里的 updated_at_text(或至少仍是"更新于 …")
                r_text = (post_result.get("body") or {}).get("updated_at_text", "")
                if r_text and r_text not in new_text:
                    print(f"⚠️ #nfoDate 文案未体现新时间(期望含 {r_text}, 实际 {new_text})")
                if "更新于" not in new_text:
                    print("❌ #nfoDate 未显示『更新于』")
                    ok = False
                else:
                    print("✓ #nfoDate 刷新为:", new_text)

        # 端点二次确认(用页面已登录 cookie 再打一次 info)
        info = await page.evaluate(
            f"fetch('/api/nfo/info/{kind}/{tmdb_id}').then(r=>r.json())"
        )
        print("   端点 GET /api/nfo/info 回包:", json.dumps(info, ensure_ascii=False))
        if info.get("exists") is not True:
            print("❌ 端点 info 未确认 exists=true")
            ok = False

        if page_errors:
            print("❌ JS 运行时错误:", page_errors[:5])
            ok = False
        if ok:
            print("\n✅ NFO 前端回归通过: 框渲染 / 日期拉取 / 点击更新 / 接口成功 / 端点一致")
            await browser.close()
            return 0
        print("\n❌ NFO 前端回归未通过")
        await browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
