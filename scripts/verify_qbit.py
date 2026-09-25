"""Playwright 验证: 详情页 TMDB/Jellyfin 跳转 + 推送 Qbit 按钮 + 管理页「下载」子页签。
桌面(1440x900) + 移动(390x844 is_mobile) 双端。登录走真实 POST /api/auth/login 拿 cookie。
"""
import sys, json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
USER, PWD = "admin", "change_me"
TV_TMDB = 37854   # 航海王(库内, 有 jfItemId/jfUrl)

def login(p):
    # 真实登录拿 cookie(后端 token 进程内随机, 不能伪造)
    r = p.request.post(f"{BASE}/api/auth/login",
                       data={"username": USER, "password": PWD})
    assert r.status == 200, f"login {r.status}"

def run(viewport, is_mobile, out_prefix):
    results = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport=viewport, is_mobile=is_mobile,
                            has_touch=is_mobile, device_scale_factor=2)
        p = ctx.new_page()
        js_errs = []
        p.on("console", lambda m: js_errs.append(m.text) if m.type == "error" else None)
        p.on("pageerror", lambda e: js_errs.append(str(e)))
        login(ctx)
        p.goto(BASE, wait_until="networkidle")
        p.wait_for_timeout(400)

        # 1) 打开详情页(库内 tv)
        p.evaluate(f"openDetailLocal('tv',{TV_TMDB},true)")
        p.wait_for_selector(".dhead-title", timeout=15000)
        # 等磁力列表渲染出来(双查询并行较慢, 给 20s)
        try:
            p.wait_for_selector(".res .push.qbit", timeout=20000)
        except Exception:
            pass
        p.wait_for_timeout(300)

        # 2) 断言: facts 里有 TMDB ID
        facts_txt = p.eval_on_selector_all(".dhead-facts .fact",
                                           "els => els.map(e=>e.innerText)")
        results["facts"] = facts_txt
        results["has_tmdb_fact"] = any("TMDB" in f and str(TV_TMDB) in f for f in facts_txt)

        # 3) 断言: 外部跳转行(TMDB + Jellyfin)
        links = p.eval_on_selector_all(".dhead-links a",
            "els => els.map(e=>({txt:e.innerText, href:e.href}))")
        results["ext_links"] = links
        results["has_tmdb_link"] = any("themoviedb.org" in l["href"] for l in links)
        results["has_jf_link"] = any("details/" in l["href"] for l in links)

        # 4) 断言: 磁力列表有「推送 Qbit」按钮
        qbit_btns = p.eval_on_selector_all(".res .push.qbit",
            "els => els.map(e=>e.innerText)")
        results["qbit_btn_count"] = len(qbit_btns)
        results["qbit_btn_texts"] = qbit_btns[:3]

        p.screenshot(path=f"/tmp/ma_qbit_{out_prefix}_detail.png")

        # 5) 关闭详情 → 管理页 → 下载子页签
        p.evaluate("closeModal()")
        p.wait_for_timeout(300)
        p.evaluate("switchTab('manage','download')")
        p.wait_for_selector("#manageBody .dl-cfg", timeout=10000)
        p.wait_for_timeout(600)
        dl_html = p.eval_on_selector("#manageBody", "e=>e.innerText")
        results["dl_has_config_form"] = "QBitTorrent 下载器" in dl_html
        results["dl_has_note"] = "尚未配置" in dl_html or "配置" in dl_html
        results["dl_url_input"] = p.eval_on_selector("#dlUrl", "e=>e.value")
        results["dl_subtab_active"] = p.eval_on_selector(
            "#manageSubtabs button[data-sub='download']", "e=>e.className")
        p.screenshot(path=f"/tmp/ma_qbit_{out_prefix}_downloads.png", full_page=True)

        # 6) 收集 JS 错误(过滤 favicon/网络 404 噪声)
        results["js_errors"] = [e for e in js_errs
                                if "favicon" not in e and "404" not in e][:10]
        b.close()
    return results

if __name__ == "__main__":
    allr = {}
    allr["desktop"] = run({"width":1440,"height":900}, False, "desktop")
    allr["mobile"] = run({"width":390,"height":844}, True, "mobile")
    print(json.dumps(allr, ensure_ascii=False, indent=2))
