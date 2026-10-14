#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whatslink 种子内容预览客户端回归(lib/whatslink.py)

用途: 磁力搜索列表「点开大图弹窗」时去第三方 whatslink.info 取**种子内容预览截图**。
本脚本锁死它的契约与坑:
  ① hash 归一化(40 位 hex / 磁力链 / 大小写); **必须构造成带 `magnet:?xt=urn:btih:`
     前缀的完整磁力** —— 上游拿裸 hash 会静默返回 `type=UNKNOWN` + 0 张截图(不报错);
  ② 上游 JSON 归一化: 有截图 → `ok=True`; 接口通但没截图 → `reason=no_screenshots`;
     缺字段不炸;
  ③ 缓存档位: **有截图 24h / 无图 6h / 上游失败 5min**, 过期即读不到;
  ④ `fetch` 端到端(打桩上游): 首次打上游、二次命中缓存不再打; bad_hash 不打上游;
  ⑤ 上游失败 → 降级成 `upstream_error` 且只缓 5min(让用户重试有机会成功);
  ⑥ **并发去重**: 同一 hash 被并发请求(连点/关了再开)只打一次上游;
  ⑦ 惰性清理: 过期文件被 `purge_expired()` 删掉, 未过期的留着;
  ⑧ 已纳入管理页「作业与缓存」的缓存表(id=`preview`)。

隔离: 全程用**临时缓存目录**(改模块的 `_CACHE_DIR`), 不碰 data/whatslink_cache;
     上游用打桩函数替换, **不发任何真实网络请求**。
用法: ./venv/bin/python scripts/verify_whatslink.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 统计用例(snapshot)会惰性读 DB —— 指到临时库, 免得碰真实 data/media_auto.db。
# ⚠️ 必须在 import 任何 db.* 之前设好(见 scripts/verify_group_rank.py 的同类说明)。
_TMPDB = tempfile.mkdtemp(prefix="media_auto_whatslink_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDB, "verify.db")

from lib import whatslink            # noqa: E402
from lib import cache_stats          # noqa: E402

FAILED = []
H = "cdf4cfe6fecefad9a531f617fcc7a81feb33fc4c"
_SHOT = "https://whatslink.info/image/9c6be0520c21056be94df45bb13fcfef"


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def _use_tmp_cache():
    """换一个干净缓存目录(模块级 _CACHE_DIR 运行期可改)。"""
    d = tempfile.mkdtemp(prefix="wa_cache_")
    whatslink._CACHE_DIR = d
    return d


def _run():
    print("-- ① hash 归一化 / 磁力构造 --")
    check("40 位 hex 原样", whatslink.normalize_hash(H) == H)
    check("大写转小写", whatslink.normalize_hash(H.upper()) == H)
    check("从磁力链提取", whatslink.normalize_hash("magnet:?xt=urn:btih:" + H) == H)
    check("非法输入返回空", whatslink.normalize_hash("hello world") == "")
    check("空输入返回空", whatslink.normalize_hash("") == "")
    check("带 tracker 的磁力链也能提取",
          whatslink.normalize_hash(f"magnet:?xt=urn:btih:{H}&dn=x&tr=udp%3A%2F%2Ft") == H)
    check("⚠️ 构造的磁力必须带 xt=urn:btih: 前缀(裸 hash 会被上游判 UNKNOWN)",
          whatslink.build_magnet(H) == "magnet:?xt=urn:btih:" + H)

    print("-- ② 上游 JSON 归一化 --")
    n = whatslink._normalize({
        "type": "FOLDER", "file_type": "folder", "size": 123, "count": 2, "name": "x",
        "screenshots": [{"time": 0, "screenshot": _SHOT},
                        {"time": 3, "screenshot": "https://whatslink.info/image/b"}]})
    check("有截图 → ok=True 且摘出地址", n["ok"] and n["screenshots"] == [
        _SHOT, "https://whatslink.info/image/b"])
    check("元信息带回来", n["type"] == "FOLDER" and n["size"] == 123 and n["count"] == 2)
    e = whatslink._normalize({"type": "UNKNOWN", "screenshots": []})
    check("接口通但没截图 → no_screenshots",
          (not e["ok"]) and e["reason"] == "no_screenshots")
    e2 = whatslink._normalize({})
    check("缺字段不炸", e2["ok"] is False and e2["screenshots"] == [])

    print("-- ③ 缓存档位与过期 --")
    whatslink.write_cache(H, {"ok": True, "screenshots": [_SHOT]}, whatslink._TTL_OK)
    check("刚写入就命中", whatslink.read_cache(H) is not None)
    check("有截图档 = 24h", whatslink._TTL_OK == 24 * 3600)
    check("24h 档: 23.9h 仍新鲜",
          whatslink.read_cache(H, now=time.time() + whatslink._TTL_OK - 60) is not None)
    check("24h 档: 超 24h 过期",
          whatslink.read_cache(H, now=time.time() + whatslink._TTL_OK + 60) is None)
    check("无图档 = 6h", whatslink._TTL_EMPTY == 6 * 3600)
    whatslink.write_cache(H, {"ok": False, "screenshots": []}, whatslink._TTL_EMPTY)
    check("6h 档: 3h 新鲜", whatslink.read_cache(H, now=time.time() + 3 * 3600) is not None)
    check("6h 档: 7h 过期", whatslink.read_cache(H, now=time.time() + 7 * 3600) is None)
    check("失败档 = 5min", whatslink._TTL_FAIL == 5 * 60)
    check("不同 hash 不串档", whatslink.read_cache("f" * 40) is None)

    print("-- ④ fetch 端到端(打桩上游) --")
    calls = []

    async def fake_ok(magnet):
        calls.append(magnet)
        return {"type": "FOLDER", "file_type": "folder", "size": 1, "count": 1, "name": "n",
                "screenshots": [{"time": 0, "screenshot": _SHOT}]}, ""

    whatslink._get_json = fake_ok
    H1 = "a" * 40
    d1 = asyncio.run(whatslink.fetch(H1))
    check("首次: 打了上游并返回截图", len(calls) == 1 and d1["ok"]
          and d1["screenshots"] == [_SHOT])
    check("⚠️ 上游收到的是完整磁力链(不是裸 hash)",
          calls[0] == "magnet:?xt=urn:btih:" + H1)
    d2 = asyncio.run(whatslink.fetch(H1))
    check("第二次: 命中缓存, 不再打上游", len(calls) == 1 and d2 == d1)

    print("-- ⑤ 非法输入不打上游 --")
    before = len(calls)
    bad = asyncio.run(whatslink.fetch("not-a-valid-hash"))
    check("bad_hash 直接返回且不打上游",
          bad["reason"] == "bad_hash" and len(calls) == before)
    bad2 = asyncio.run(whatslink.fetch(""))
    check("空串同理", bad2["reason"] == "bad_hash" and len(calls) == before)

    print("-- ⑥ 上游失败降级(只缓 5 分钟) --")
    H2 = "1" * 40

    async def fake_fail(magnet):
        return None, "直连 boom; 代理 boom"

    whatslink._get_json = fake_fail
    f = asyncio.run(whatslink.fetch(H2))
    check("失败 → ok=False / reason=upstream_error",
          (not f["ok"]) and f["reason"] == "upstream_error")
    check("失败带上错误原因(便于排查)", "boom" in (f.get("error") or ""))
    check("失败结果被缓存(短期)",
          whatslink.read_cache(H2) is not None
          and whatslink.read_cache(H2, now=time.time() + whatslink._TTL_FAIL + 60) is None)
    check("缓存里存的是失败档位而非 24h",
          whatslink.read_cache(H2, now=time.time() + 3600) is None)

    print("-- ⑦ 接口通但没截图(6h 档) --")
    H3 = "2" * 40

    async def fake_empty(magnet):
        return {"type": "UNKNOWN", "screenshots": []}, ""

    whatslink._get_json = fake_empty
    z = asyncio.run(whatslink.fetch(H3))
    check("reason=no_screenshots", z["reason"] == "no_screenshots" and z["ok"] is False)
    check("空结果缓存 6h",
          whatslink.read_cache(H3) is not None
          and whatslink.read_cache(H3, now=time.time() + whatslink._TTL_EMPTY + 60) is None)

    print("-- ⑧ 并发去重(同一 hash 只打一次上游) --")
    H4 = "3" * 40
    ncalls = []

    async def slow_ok(magnet):
        ncalls.append(magnet)
        await asyncio.sleep(0.08)
        return {"screenshots": [{"time": 0, "screenshot": _SHOT}]}, ""

    whatslink._get_json = slow_ok

    async def both():
        return await asyncio.gather(whatslink.fetch(H4), whatslink.fetch(H4))

    ra, rb = asyncio.run(both())
    check("两个并发只打一次上游", len(ncalls) == 1, f"实际 {len(ncalls)} 次")
    check("两个调用拿到同一个结果", ra == rb and ra["ok"])

    print("-- ⑨ 惰性过期清理 --")
    H5 = "4" * 40
    whatslink.write_cache(H5, {"ok": True, "screenshots": [_SHOT]}, 1)
    whatslink.write_cache("5" * 40, {"ok": True, "screenshots": [_SHOT]}, 3600)
    removed = whatslink.purge_expired(now=time.time() + 10)
    check("过期的被清掉", removed >= 1, f"删除 {removed} 个")
    check("清完读不到", whatslink.read_cache(H5) is None)
    check("未过期的留着", whatslink.read_cache("5" * 40) is not None)

    print("-- ⑩ 纳入「作业与缓存」统计 --")
    ids = [c["id"] for c in cache_stats.snapshot()]
    check("preview 出现在缓存表里", "preview" in ids, str(ids))
    row = [c for c in cache_stats.snapshot() if c["id"] == "preview"][0]
    check("键数取自真实目录(不是 0 占位)", row["stats"]["keys"] >= 1,
          str(row["stats"]))
    check("可被「清除缓存」清理(flusher 已注册)",
          cache_stats.flush("preview") is not None)


def main():
    # 临时库建表 —— 否则 cache_stats.snapshot() 读 tmdb_media 时会刷一堆"无此表"错误
    from db.database import init_db
    init_db()
    cache_dir = _use_tmp_cache()
    try:
        _run()
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)
        shutil.rmtree(_TMPDB, ignore_errors=True)
    print()
    if FAILED:
        print(f"-- 检查结果 --\n  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("-- 检查结果 --\n  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
