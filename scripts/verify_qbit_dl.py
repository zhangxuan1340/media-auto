"""验证下载子页签【带任务数据】渲染: 全局统计 + 作品聚合(集数徽章) + 任务明细进度条。
前置: config.qbit 已指向 mock qbit(127.0.0.1:8099)。桌面+移动截图。
"""
import json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
USER, PWD = "admin", "change_me"


def run(viewport, is_mobile, prefix):
    res = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport=viewport, is_mobile=is_mobile,
                            has_touch=is_mobile, device_scale_factor=2)
        p = ctx.new_page()
        errs = []
        p.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        p.on("pageerror", lambda e: errs.append(str(e)))
        ctx.request.post(f"{BASE}/api/auth/login", data={"username": USER, "password": PWD})
        p.goto(BASE, wait_until="networkidle")
        p.wait_for_timeout(300)
        p.evaluate("switchTab('manage','download')")
        p.wait_for_selector("#manageBody .dl-stats", timeout=10000)
        p.wait_for_timeout(800)

        res["stats"] = p.eval_on_selector_all(".dl-stat",
            "els=>els.map(e=>e.innerText.replace(/\\n/g,' '))")
        res["show_count"] = p.eval_on_selector_all(".dl-show", "els=>els.length")
        res["show_titles"] = p.eval_on_selector_all(".dl-show-title", "els=>els.map(e=>e.innerText)")
        res["ep_badges"] = p.eval_on_selector_all(".dl-ep",
            "els=>els.map(e=>({t:e.innerText,c:e.className}))")
        res["show_max_ep"] = p.eval_on_selector_all(".dl-show-ep", "els=>els.map(e=>e.innerText)")
        res["torrent_count"] = p.eval_on_selector_all(".dl-torrent", "els=>els.length")
        res["torrent_names"] = p.eval_on_selector_all(".dl-torrent-name", "els=>els.map(e=>e.innerText)")
        res["js_errors"] = [e for e in errs if "favicon" not in e and "404" not in e][:8]
        p.screenshot(path=f"/tmp/ma_qbit_{prefix}_dl.png", full_page=True)
        b.close()
    return res


if __name__ == "__main__":
    out = {"desktop": run({"width":1440,"height":900}, False, "dl_desktop"),
           "mobile": run({"width":390,"height":844}, True, "dl_mobile")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
