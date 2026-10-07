#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""media-auto / verify_imgproxy —— 图片本地缓存代理 /api/img 回归

背景(2026-10-07 用户报障「种子搜索绝大多数没有图」的后半段):
  Jackett 的 Torznab 结果用 <torznab:attr name="coverurl"> 给封面, 但那个地址指向
  **Jackett 自身**(常在内网/私有网段), 浏览器跨网直连必然裂图。修法 = 后端把 coverurl
  改写成同源 /api/img/<token>, 由服务端代取; 白名单按配置的 jackett.base **动态放行**。

本脚本锁死:
  ① token 编解码往返(base64url, 无 '=' 补位); proxy_url 空串原样返回、可解码回原址;
  ② 代理白名单: TMDB 图片放行; Jackett base **从运行期配置动态读取**(http/https 两种),
     其它主机一律拒绝; 端口相近(jackett.test:9117 vs :91170)不被前缀误放行;
  ③ 后端改写: Jackett 结果的 coverurl → /api/img/<token>(可解码回原址), 无封面仍是空串;
     `_search_jackett` 与 `_fetch_all` 两条取数路径同口径;
  ④ 未配置 jackett.base 时只放行 TMDB(不因配置缺失就放开任意主机);
  ⑤ 源码里**不含**内网地址(禁把 172.16.x 之类写死进仓库)。

用法: ./venv/bin/python scripts/verify_imgproxy.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* / server.* 之前指好库路径(db/database.py 导入时就建 engine)
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_imgproxy_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import init_db            # noqa: E402
from lib.config import save_config         # noqa: E402

FAILED = []
JACKETT_BASE = "http://jackett.test:9117"
COVER = JACKETT_BASE + "/img/yts/cover.jpg"
TMDB = "https://image.tmdb.org/t/p/w500/poster.jpg"


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def reset_whitelist_cache(imgproxy):
    """清掉白名单的 TTL 缓存 —— 让配置改动在断言里立刻生效(生产靠 5 分钟自然过期)。"""
    imgproxy._jackett_cache["ts"] = 0.0
    imgproxy._jackett_cache["prefixes"] = ()


def decoded_local(local_path):
    """把 /api/img/<token> 解回上游地址(测试辅助)。"""
    return local_path[len("/api/img/"):]


def main():
    init_db()
    save_config({"jackett": {"base": JACKETT_BASE, "indexer": ["yts"]}})

    from server import imgproxy
    from server import config as srvcfg
    from server.routers import search as s
    from scripts import jackett_search

    print("-- ① token 编解码 --")
    u = "https://image.tmdb.org/t/p/w500/中文 海报.jpg"
    tok = imgproxy._enc(u)
    check("往返一致(含中文/空格)", imgproxy._dec(tok) == u)
    check("base64url 无 '=' 补位", "=" not in tok)
    check("proxy_url('') 原样返回空", imgproxy.proxy_url("") == "")
    check("proxy_url 指向 /api/img/", imgproxy.proxy_url(u).startswith("/api/img/"))
    check("proxy_url 可解码回原址",
          imgproxy._dec(decoded_local(imgproxy.proxy_url(u))) == u)

    print("-- ② 代理白名单(按配置动态放行) --")
    srvcfg.reload_config()
    reset_whitelist_cache(imgproxy)
    check("TMDB 图片放行", imgproxy._allowed(TMDB))
    check("Jackett http 放行", imgproxy._allowed(COVER))
    check("Jackett https 放行(同主机换协议)", imgproxy._allowed("https://jackett.test:9117/img/yts/c.jpg"))
    check("其它主机拒绝", not imgproxy._allowed("http://evil.example.com/a.jpg"))
    check("端口相近不误放行(9117 vs 91170)",
          not imgproxy._allowed("http://jackett.test:91170/img/yts/c.jpg"))
    check("配置里的 base 出现在前缀清单", JACKETT_BASE + "/" in imgproxy._allowed_prefixes())

    print("-- ③ 后端改写(Jackett 封面 → 同源代理) --")
    def fake_search(cfg, q, limit):
        return [
            {"hash": "a" * 40, "name": "YTS 有封面", "size": 10 ** 9, "seeders": 3,
             "leechers": 1, "magnet": "magnet:?xt=urn:btih:" + "a" * 40, "image": COVER},
            {"hash": "b" * 40, "name": "TPB 无封面", "size": 10 ** 8, "seeders": 1,
             "leechers": 0, "magnet": "magnet:?xt=urn:btih:" + "b" * 40, "image": ""},
        ]
    jackett_search.search = fake_search

    out, _more, _nxt = asyncio.run(s._search_jackett({}, "x", 30))
    check("单源搜索: coverurl 改写成本地代理", out[0]["image"].startswith("/api/img/"))
    check("单源搜索: 改写可解码回原址",
          imgproxy._dec(decoded_local(out[0]["image"])) == COVER)
    check("单源搜索: 改写后的地址被白名单放行",
          imgproxy._allowed(imgproxy._dec(decoded_local(out[0]["image"]))))
    check("单源搜索: 无封面仍是空串", out[1]["image"] == "")

    items = asyncio.run(s._fetch_all("jackett", {}, "x", 100))
    check("聚合取数: 同样改写", items[0]["image"].startswith("/api/img/"))
    check("聚合取数: 无封面仍是空串", items[1]["image"] == "")

    print("-- ④ 未配置 jackett.base 的退化行为 --")
    save_config({"jackett": {"base": "", "indexer": ["yts"]}})
    srvcfg.reload_config()
    reset_whitelist_cache(imgproxy)
    check("未配 base: TMDB 仍放行", imgproxy._allowed(TMDB))
    check("未配 base: 不放开任意主机", not imgproxy._allowed(COVER))

    print("-- ⑤ 不把内网地址写死进源码 --")
    src = open(imgproxy.__file__, encoding="utf-8").read()
    leak = [s for s in ("172.16.", "192.168.", "10.0.", "127.0.0.1") if s in src]
    check("imgproxy 源码无内网地址硬编码", not leak, f"发现: {leak}")
    # 注释/docstring 里出现地址形态也只是说明, 这里只拦"真正的写死"——前缀表必须来自配置
    check("前缀表来自配置(非硬编码常量)",
          JACKETT_BASE not in src)

    print()
    if FAILED:
        print(f"检查结果 -- 失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("-- 检查结果 --\n  全部通过")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
    sys.exit(code)
