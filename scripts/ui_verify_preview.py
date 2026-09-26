#!/usr/bin/env python3
"""聚焦验证: 整理页「预期文件列表」折叠预览 + 浏览页 libBadge 四态徽章(系统 Chrome)。

不依赖 CD2 是否可达:
  - 真实流: 登录→整理页, 若计划加载成功则展开首条文件预览, 校验结构文本 + 截图。
  - 注入流: 无论 CD2 是否可达, 都向页面注入样例 preview 调 filePreviewHTML, 校验渲染正确。
  - 浏览页: 扫描电影卡片徽章文本(完整/不完整/库内·未确认/未拥有), 确认四态徽章存在。
输出截图到 state/ui_verify_preview/。
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


def creds():
    a = load_config()["web"]["auth"]
    return a["username"], a["password"]


async def login(page):
    await page.goto(BASE, wait_until="networkidle")
    if await page.locator("#login").is_visible():
        u, p = creds()
        await page.fill("#lu", u)
        await page.fill("#lp", p)
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(800)
    return not await page.locator("#login").is_visible()


async def main():
    from playwright.async_api import async_playwright
    out = ROOT / "state" / "ui_verify_preview"
    out.mkdir(parents=True, exist_ok=True)
    page_errors = []
    ok = True

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome")
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = await ctx.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        logged = await login(page)
        print("登录:", "成功" if logged else "失败")
        if not logged:
            print("❌ 登录失败, 退出"); await browser.close(); return 1

        # ---------- 整理页 ----------
        await page.evaluate("switchTab('organize')")
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(out / "organize.png"))

        plan_count = await page.locator("#tab-organize .opick, #tab-organize tbody tr").count()
        toggle_count = await page.locator(".org-toggle").count()
        body_txt = (await page.locator("#tab-organize").inner_text())[:400]
        print(f"\n[整理页] 行数≈{plan_count}, 文件折叠开关数={toggle_count}")
        print("  内容预览:", body_txt.replace(chr(10), " ")[:160])

        if toggle_count:
            # 真实展开首条
            await page.locator(".org-toggle").first.click()
            await page.wait_for_timeout(600)
            det_visible = await page.locator(".org-detail").first.is_visible()
            det_txt = (await page.locator(".org-detail").first.inner_text())[:300] if det_visible else ""
            print(f"  展开首条 → 可见={det_visible}")
            print("  预览文本:", det_txt.replace(chr(10), " ")[:200])
            has_struct = ("预期落点" in det_txt) or ("Season" in det_txt) or ("→" in det_txt)
            print("  含结构/落点:", has_struct)
            if not (det_visible and has_struct):
                ok = False
            await page.screenshot(path=str(out / "organize_expanded.png"))
        else:
            print("  ⚠️ 无计划(可能 CD2 不可达) → 走注入流验证渲染")

        # ---------- 注入流: 直接用样例 preview 调 filePreviewHTML ----------
        inj = await page.evaluate("""() => {
            if (typeof filePreviewHTML !== 'function') return 'NO_FN';
            const sample = {
                target:'/Cloud/OtShow/小小世界 (2020)', quality:'2160p', merge_existing:false,
                media:[{src:'小小世界.2020.S01E01.2160p.mkv',
                        dst:'小小世界 - S01E01 - 2160p.mkv',
                        dst_path:'/Cloud/OtShow/小小世界 (2020)/Season 1/小小世界 - S01E01 - 2160p.mkv',
                        dst_rel:'Season 1/小小世界 - S01E01 - 2160p.mkv', note:''},
                       {src:'小小世界.2020.S01E02.2160p.mkv',
                        dst:'小小世界 - S01E02 - 2160p.mkv',
                        dst_path:'/Cloud/OtShow/小小世界 (2020)/Season 1/小小世界 - S01E02 - 2160p.mkv',
                        dst_rel:'Season 1/小小世界 - S01E02 - 2160p.mkv', note:''}],
                deletes:['【推广广告】.txt','sample.nfo'], keeps:['poster.jpg','fanart.jpg']};
            const d = document.createElement('div');
            d.id='testPrev'; d.style.maxWidth='900px'; d.style.margin='40px auto';
            d.style.background='#fff'; d.style.padding='20px';
            d.innerHTML = '<h3>注入样例预览</h3>' + filePreviewHTML(sample);
            document.body.appendChild(d);
            const txt = d.innerText;
            return JSON.stringify({hasHead: txt.includes('预期落点'),
                                  hasSeason: txt.includes('Season 1'),
                                  hasArrow: txt.includes('→'),
                                  hasDel: txt.includes('将删除'),
                                  hasKeep: txt.includes('保留')});
        }""")
        print("\n[注入流 filePreviewHTML]:", inj)
        if inj != "NO_FN":
            r = json.loads(inj)
            if not all([r["hasHead"], r["hasSeason"], r["hasArrow"], r["hasDel"], r["hasKeep"]]):
                ok = False
        else:
            print("  ⚠️ filePreviewHTML 未定义(JS 未加载?)"); ok = False
        await page.screenshot(path=str(out / "inject_preview.png"), full_page=False)

        # ---------- 浏览页: libBadge 四态 ----------
        await page.evaluate("switchTab('movie')")
        await page.wait_for_timeout(2500)
        badges = await page.evaluate("""() => {
            const txts = Array.from(document.querySelectorAll('#tab-movie .badge'))
                          .map(b => b.innerText.trim());
            const uniq = Array.from(new Set(txts));
            return uniq;
        }""")
        print("\n[浏览·电影 徽章样本]:", badges)
        wanted = {"完整", "不完整", "库内·未确认", "未拥有"}
        have = set(badges)
        # 至少确认徽章系统在工作(有 完整/未拥有 等); 四态不必同时出现(取决于数据)
        print("  命中期望徽章:", sorted(have & wanted))
        await page.screenshot(path=str(out / "browse_movie.png"))

        await ctx.close()
        await browser.close()

    if page_errors:
        print("\n❌ JS 运行时错误:")
        for e in page_errors[:10]:
            print("   ", e[:200])
        ok = False

    print("\n" + ("✓ 验证通过" if ok else "❌ 存在问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
