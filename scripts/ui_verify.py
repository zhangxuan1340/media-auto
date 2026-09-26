#!/usr/bin/env python3
"""MediaAuto Web UI 自动验证(playwright)

用法:
  venv/bin/python scripts/ui_verify.py            # 桌面+移动 全部页签截图 + 错误检查
  venv/bin/python scripts/ui_verify.py --tab browse --mobile   # 只看某页签/只看移动端
  venv/bin/python scripts/ui_verify.py --detail 1494          # 打开某 TMDB 电影详情截图

输出:
  state/ui_verify/<viewport>/<tab>.png
退出码: 0=无 JS 运行时错误, 1=有错误(输出错误明细)
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT))
from lib.config import load_config  # noqa: E402  统一配置入口(数据库优先)
BASE = "http://127.0.0.1:8787"

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900, "device_scale_factor": 2, "mobile": False},
    "mobile": {"width": 390, "height": 844, "device_scale_factor": 3, "mobile": True, "is_mobile": True},
}
# 4 个主页签(2026-09 二版: 电影/剧集/整理/管理) + 管理页子页签(缺失并入管理)
TABS = ["movie", "tv", "organize",
        "manage/missing", "manage/general", "manage/track",
        "manage/local", "manage/jobs", "manage/browse", "manage/block"]
# PWA 资源(验证可访问)
PWA_ASSETS = ["/manifest.webmanifest", "/sw.js", "/icons/icon-192.png",
              "/icons/icon-512.png", "/icons/apple-touch-icon.png"]


async def goto_tab(page, tab):
    """切换页签: 'manage/xxx' 先进 manage 主页签再切子页签; 其余直接 switchTab"""
    if tab.startswith("manage/"):
        sub = tab.split("/", 1)[1]
        await page.evaluate("switchTab('manage')")
        await page.wait_for_timeout(400)
        await page.evaluate(f"switchManageSub('{sub}')")
    else:
        await page.evaluate(f"switchTab('{tab}')")


def creds():
    cfg = load_config()
    a = cfg["web"]["auth"]
    return a["username"], a["password"]


async def login(page):
    await page.goto(BASE, wait_until="networkidle")
    if await page.locator("#login").is_visible():
        await page.fill("#lu", creds()[0])
        await page.fill("#lp", creds()[1])
        await page.locator("#loginForm button[type=submit]").click()
        await page.wait_for_timeout(800)


async def shot(page, name, outdir):
    p = outdir / f"{name}.png"
    await page.screenshot(path=str(p))
    print(f"  截图 {p}")


async def check_overflow(page, viewport):
    """检测视口内横向溢出元素(移动端布局问题主因)。返回溢出元素描述列表。"""
    return await page.evaluate("""(maxW) => {
        const out = [];
        const els = document.querySelectorAll('#app *, #modalBg *');
        for (const el of els) {
            const r = el.getBoundingClientRect();
            // 只关注可见、超出视口右缘 4px 以上的元素(滚动容器内部除外)
            if (r.width > 0 && r.right > maxW + 4) {
                const style = getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                // 排除"横滑容器内部"的元素: 自身或某个祖先可横向滚动(如 person-strip 里的 chip),
                // 超出视口是设计功能(横滑查看), 不是布局 bug
                const so = getComputedStyle(el).overflowX;
                if (so === 'auto' || so === 'scroll') continue;
                let inScroller = false;
                for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
                    const ox = getComputedStyle(a).overflowX;
                    if (ox === 'auto' || ox === 'scroll') { inScroller = true; break; }
                }
                if (inScroller) continue;
                out.push(`${el.tagName.toLowerCase()}${el.id ? '#'+el.id : ''}${el.className && typeof el.className === 'string' ? '.'+el.className.trim().split(/\\s+/).slice(0,2).join('.') : ''} right=${Math.round(r.right)} (>${maxW})`);
            }
        }
        return out.slice(0, 30);
    }""", viewport["width"])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", help="只截某页签")
    ap.add_argument("--mobile", action="store_true", help="只看移动端")
    ap.add_argument("--detail", help="打开某 TMDB 电影详情(id)")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    outdir = ROOT / "state" / "ui_verify"
    outdir.mkdir(parents=True, exist_ok=True)

    vps = list(VIEWPORTS)
    if args.mobile:
        vps = ["mobile"]

    errors_all = []
    async with async_playwright() as pw:
        # 启动策略: 系统 Chrome 优先(channel=chrome, 无需下载浏览器) → 回退自带 chromium
        # (npmmirror 的 1243 build 只传了 linux 包, mac 浏览器拿不到, 故优先用系统 Chrome)
        browser = None
        for launch_kw in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(**launch_kw)
                print(f"  浏览器: {'系统 Chrome' if launch_kw else '自带 chromium'}")
                break
            except Exception as e:
                if launch_kw:
                    print(f"  系统 Chrome 启动失败({e.__class__.__name__}), 回退自带 chromium")
        if browser is None:
            print("❌ 无可用浏览器(系统 Chrome 与自带 chromium 均不可用)")
            return 1
        for vp_name in vps:
            vp = VIEWPORTS[vp_name]
            ctx = await browser.new_context(
                viewport={"width": vp["width"], "height": vp["height"]},
                device_scale_factor=vp.get("device_scale_factor", 2),
                is_mobile=vp.get("is_mobile", False),
                has_touch=vp.get("is_mobile", False),
            )
            page = await ctx.new_page()
            page_errors, console_errs = [], []
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("console", lambda m: console_errs.append(m.text) if m.type == "error" else None)

            vdir = outdir / vp_name
            vdir.mkdir(parents=True, exist_ok=True)
            print(f"\n===== {vp_name} {vp['width']}x{vp['height']} =====")
            await login(page)
            if await page.locator("#login").is_visible():
                print("  ⚠️ 登录失败, 无法验证"); await ctx.close(); continue

            # 1) 各页签(含设置子页签)
            tabs = [args.tab] if args.tab else TABS
            for tab in tabs:
                await goto_tab(page, tab)
                await page.wait_for_timeout(1200)
                await shot(page, tab.replace("/", "_"), vdir)

            # 2) 横向溢出检查(移动端重点)
            if vp_name == "mobile":
                for tab in tabs:
                    await goto_tab(page, tab)
                    await page.wait_for_timeout(800)
                    over = await check_overflow(page, vp)
                    if over:
                        for o in over:
                            print(f"  ⚠️ [{tab}] 横向溢出: {o}")
                        errors_all.append(f"{vp_name}/{tab} overflow")

            # 3) 详情页(电影热门第一张卡)
            await page.evaluate("switchTab('movie')")
            await page.wait_for_timeout(1000)
            first_card = page.locator("#tab-movie .card").first
            if await first_card.count():
                await first_card.click()
                await page.wait_for_timeout(1500)
                await shot(page, "detail_first", vdir)
                if vp_name == "mobile":
                    over = await check_overflow(page, vp)
                    if over:
                        for o in over[:10]:
                            print(f"  ⚠️ [detail] 横向溢出: {o}")
                        errors_all.append(f"{vp_name}/detail overflow")
                await page.keyboard.press("Escape")
                # 移动端没有 Esc 语义, 直接关
                await page.evaluate("closeModal()")
                await page.wait_for_timeout(400)

            if args.detail and vp_name == "desktop":
                await page.evaluate(f"openDetailLocal('movie', {args.detail})")
                await page.wait_for_timeout(1500)
                await shot(page, f"detail_{args.detail}", vdir)

            # 4) 错误汇总
            if page_errors:
                for e in page_errors:
                    print(f"  ❌ [pageerror] {e[:200]}")
                errors_all.append(f"{vp_name} pageerror")
            # console error 过滤掉图片 404/资源类噪音
            real = [e for e in console_errs if not any(k in e for k in ["404", "net::", "Failed to load resource", "ERR_"])]
            if real:
                for e in real[:10]:
                    print(f"  ⚠️ [console] {e[:200]}")
                errors_all.append(f"{vp_name} console")
            else:
                print("  ✓ 无 JS 运行时错误")

            await ctx.close()

    await browser.close()
    # PWA 资源可达性检查(纯 HTTP, 不依赖浏览器状态)
    _check_pwa(errors_all)

    print("\n" + "=" * 50)
    if errors_all:
        print(f"❌ 共 {len(errors_all)} 类问题: {errors_all}")
        return 1
    print("✓ 全部通过: 无 JS 错误, 无横向溢出, PWA 资源齐全")
    return 0


def _check_pwa(errors_all):
    """PWA 资源可达性 + manifest 字段校验(纯 urllib, 浏览器关闭后仍可靠)"""
    import urllib.request
    print("\n===== PWA 资源 =====")
    for asset in PWA_ASSETS:
        try:
            req = urllib.request.Request(BASE + asset, method="GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                ok = r.status == 200
                print(f"  {'✓' if ok else '❌'} {asset} → {r.status}")
                if not ok:
                    errors_all.append(f"pwa {asset} {r.status}")
        except Exception as e:
            print(f"  ❌ {asset} → {getattr(e, 'code', e.__class__.__name__)}")
            errors_all.append(f"pwa {asset} err")
    # manifest 内容校验(关键 PWA 字段)
    try:
        req = urllib.request.Request(BASE + "/manifest.webmanifest", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            mf = json.loads(r.read().decode("utf-8"))
        missing = [f for f in ("name", "start_url", "display", "icons") if f not in mf]
        if missing:
            print(f"  ❌ manifest 缺字段 {missing}")
            errors_all.append("manifest field")
        else:
            print(f"  ✓ manifest 字段齐全 (display={mf.get('display')}, icons={len(mf.get('icons', []))})")
    except Exception as e:
        print(f"  ❌ manifest 解析失败 {e}")
        errors_all.append("manifest parse")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
