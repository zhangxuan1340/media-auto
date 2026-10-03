#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【纯本地·打桩站点】磁力搜索翻页回归: 并行抓取 + 分段窗口 + 加载更多语义

报障(2026-09-30): 详情页看种子"页面很慢、操作不顺畅、加载更多点了没反应"。
根因(实测): 站点每页 10 条要 2~3.5s, 老实现
  ① 非 relevance(默认质量优先)每个请求都拉满 _SORT_CAP=200 条 → 串行翻 20 页 = 12~20s 首屏;
  ② 排序结果缓存只有 120s → 过期后点一次「加载更多」又等十几秒, 且期间旧按钮还能点;
  ③ collect() 纯串行翻页, 页与页之间没有并发。

修复: collect() 按 offset 并行翻页(默认 6 路); 非 relevance 改成**分段窗口**
(首屏 max(need+30, 60) 条, 要更多再抓更大窗口, 到 200 封顶); 前端加首屏/加载更多互斥与到底提示。
本脚本用打桩站点验证这三件事, 不联网。

用法: ./venv/bin/python scripts/verify_search_pages.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []
LATENCY = 0.25          # 打桩站点单页往返耗时(模拟真实 2~3.5s, 这里缩到 0.25s 便于断言)
TOTAL = 64              # 语料条数(6 页满 + 第 7 页 4 条)


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


class FakeSite:
    """打桩的 diao_search.search: 按 offset 出页, 记录并发数与调用次数。"""

    def __init__(self, total=TOTAL):
        self.total = total
        self.calls = 0
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.caps = []            # _fetch_all 的 cap(经 search 侧看不出来, 另记)

    def corpus(self, i):
        return {"hash": f"{i:040d}", "name": f"Movie.2024.1080p.WEB-{i:03d}",
                "size": 1_000_000_000 + i, "magnet": f"magnet:?xt=urn:btih:{i:040d}"}

    def search(self, base, keyword, offset=0, limit=10):
        with self.lock:
            self.calls += 1
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            time.sleep(LATENCY)
            chunk = [self.corpus(i) for i in range(offset, min(offset + limit, self.total))]
            return chunk, offset + limit < self.total, self.total, [keyword]
        finally:
            with self.lock:
                self.inflight -= 1

    def reference_collect(self, want, start_offset=0):
        """串行参考实现(collect 修复前的语义), 用来对拍顺序/去重/翻页口径。"""
        out, seen, off, full = [], set(), start_offset, False
        while len(out) < want and off < self.total:
            chunk = [self.corpus(i) for i in range(off, min(off + 10, self.total))]
            for t in chunk:
                if t["hash"] in seen:
                    continue
                seen.add(t["hash"])
                out.append(t)
            full = len(chunk) >= 10
            off += len(chunk)
            if not full:
                break
        return out[:want], (full and len(out) >= want), off


def main():
    from scripts import diao_search as D
    from server.routers import search as s

    real_search = D.search
    site = FakeSite()
    D.search = site.search
    try:
        # ---------- ① collect 并行翻页: 结果与串行同序, 且确实并发 ----------
        print("\n-- ① collect 并行翻页 --")
        site.calls = 0
        site.max_inflight = 0
        t0 = time.time()
        got, tc, kw, more, end = D.collect("http://fake", "q", want=60, start_offset=0)
        dt = time.time() - t0
        ref, ref_more, ref_end = site.reference_collect(60, 0)
        check("并行 collect 结果 = 串行参考(顺序/去重)",
              [t["hash"] for t in got] == [t["hash"] for t in ref], f"{len(got)} vs {len(ref)}")
        check("has_more / end_offset 与参考一致",
              more == ref_more and end == ref_end, (more, ref_more, end, ref_end))
        check("6 页确实并发(max_inflight >= 4)", site.max_inflight >= 4, site.max_inflight)
        check(f"60 条耗时 {dt:.2f}s < 串行 6 页(1.50s) 的 2/3", dt < 1.0, f"{dt:.2f}s")
        # 不足一页 / 到底
        got2, _tc, _kw, more2, end2 = D.collect("http://fake", "q", want=200, start_offset=0)
        check("语料到底 → has_more=False, 条数=语料条数",
              more2 is False and len(got2) == TOTAL, (len(got2), more2))
        check("end_offset 停在最后一页末尾", end2 == TOTAL, end2)
        # 起始 offset 续翻(加载更多语义)
        got3, _tc, _kw, more3, end3 = D.collect("http://fake", "q", want=30, start_offset=30)
        check("start_offset=30 续翻拿到 30..59",
              [t["hash"] for t in got3] == [t["hash"] for t in ref[30:60]], len(got3))
        check("续翻 has_more=True(end=60)", more3 is True and end3 == 60, (more3, end3))
        # 全部页失败 → 抛
        def boom(*a, **k):
            raise RuntimeError("站点挂了")
        D.search = boom
        try:
            D.collect("http://fake", "q", want=30, start_offset=0)
            check("全部页失败 → 抛错(不静默给空)", False, "没有抛")
        except RuntimeError as e:
            check("全部页失败 → 抛错(不静默给空)", "站点挂了" in str(e), e)
        D.search = site.search

        # ---------- ② 非 relevance 分段窗口 ----------
        print("\n-- ② 非 relevance 分段窗口(首屏不拉满 200) --")
        s._sort_cache.clear()
        site.caps = []
        real_fetch = s._fetch_all

        async def spy_fetch(source, cfg, q, cap, *a, **k):
            # 转发 start_offset / multi 等新参到真实 _fetch_all(接口演进保持兼容)
            site.caps.append(cap)
            return await real_fetch(source, cfg, q, cap, *a, **k)

        s._fetch_all = spy_fetch
        cfg = {"bitmagnet_next_web": {"enabled": True, "base": "http://fake"},
               "search": {"group_priority": []}}
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from server.auth import require_auth
            from server.config import get_config

            app = FastAPI()
            app.include_router(s.router)
            app.dependency_overrides[require_auth] = lambda: None
            app.dependency_overrides[get_config] = lambda: cfg
            client = TestClient(app)

            r1 = client.get("/api/search?q=test&sort=quality&limit=30").json()
            check("首屏只抓一个窗口(<=60 条), 不是 _SORT_CAP=200",
                  site.caps and site.caps[0] <= s._WINDOW0, site.caps)
            check("首屏 30 条 + totalCount=窗口条数",
                  len(r1["items"]) == 30 and r1["totalCount"] == site.caps[0],
                  (len(r1["items"]), r1["totalCount"]))
            check("首屏 hasMore=True(exhausted=False)",
                  r1["hasMore"] is True and r1.get("exhausted") is False, r1.get("exhausted"))

            n_calls = site.calls
            r2 = client.get("/api/search?q=test&sort=quality&limit=30&page=2").json()
            check("第 2 页直接吃缓存(不发新抓取)", site.calls == n_calls,
                  f"{n_calls} -> {site.calls}")
            check("第 2 页拿到 30..59 的 30 条", len(r2["items"]) == 30, len(r2["items"]))
            check("第 2 页 hasMore=True(窗口没到底)", r2["hasMore"] is True, r2["hasMore"])

            r3 = client.get("/api/search?q=test&sort=quality&limit=30&page=3").json()
            # 增量续抓: 第 3 页要 90 条, 首屏窗口只有 60 → 必须再发一次抓取(增量补 target-cap0),
            # 而不是直接吃 60 条的旧缓存。用调用次数增长来验证(不耦合"单个更大 cap"的旧回退写法)。
            check("第 3 页需要 90 条 → 触发增量抓取撑大窗口", site.calls > n_calls,
                  f"calls {n_calls} -> {site.calls}")
            check("语料 64 条到底 → exhausted 且 hasMore=False",
                  r3.get("exhausted") is True and r3["hasMore"] is False,
                  (r3.get("exhausted"), r3["hasMore"]))
            check("第 3 页只回剩下的 4 条", len(r3["items"]) == 4, len(r3["items"]))
            check("第 4 页空 + 不再有更多",
                  client.get("/api/search?q=test&sort=quality&limit=30&page=4").json()
                  .get("hasMore") is False)

            # 换排序 → 换缓存 key(规则指纹进 key 的老行为不回归)
            s._sort_cache.clear()
            site.caps.clear()
            rs = client.get("/api/search?q=test&sort=size_desc&limit=30").json()
            check("size_desc 也走分段窗口", site.caps == [s._WINDOW0], site.caps)
            check("size_desc 按大小降序",
                  all((rs["items"][i]["size"] or 0) >= (rs["items"][i + 1]["size"] or 0)
                      for i in range(len(rs["items"]) - 1)),
                  [x.get("size") for x in rs["items"][:3]])

            # 相关性: 不拉窗口, 按页直取, nextPage = 实际 offset 换算
            s._sort_cache.clear()
            site.caps.clear()
            rr = client.get("/api/search?q=test&sort=relevance&limit=30").json()
            check("relevance 不进分段窗口(不调 _fetch_all)", site.caps == [], site.caps)
            check("relevance 首页 30 条 + nextPage=4(站点每页 10)",
                  len(rr["items"]) == 30 and rr["nextPage"] == 4,
                  (len(rr["items"]), rr["nextPage"]))

            # 缓存窗口过期(TTL)后重新抓 → cap 会随需要的页数增长
            s._sort_cache.clear()
            client.get("/api/search?q=test&sort=quality&limit=30")   # 重新灌一份窗口缓存
            key = s._sort_key("test", "quality", "next_web", cfg)
            ts, items, cap, ex = s._sort_cache[key]
            s._sort_cache[key] = (ts - (s._SORT_CACHE_TTL + 1), items, cap, ex)
            site.caps.clear()
            client.get("/api/search?q=test&sort=quality&limit=30&page=3")
            check("缓存过期后重新抓(且按需增长)", site.caps and site.caps[0] > s._WINDOW0,
                  site.caps)
        finally:
            s._fetch_all = real_fetch
            s._sort_cache.clear()

        # ---------- ③ 前端反馈不变量(源码层) ----------
        print("\n-- ③ 前端加载反馈 --")
        js = (ROOT / "server" / "static" / "js" / "detail.js").read_text("utf-8")
        check("首屏在拉时忽略「加载更多」(box._loading/_loadingMore 互斥)",
              "if(box._loading || box._loadingMore){ return; }" in js)
        check("新搜索清掉旧按钮与结果(避免点了被覆盖)",
              "box._loadingMore = false;\n  box._emptyStreak = 0;" in js
              or "_loadingMore = false" in js)
        check("到底给明确文案, 不静默删按钮",
              "_magEnd(box, btn);" in js and "btn.remove(); return;" not in js)
        check("整页重复 → 自动续翻 + 提示",
              "自动接着加载" in js and "_emptyStreak" in js)
        check("站点慢有提示(8s 换文案)", "magSpinTxt" in js and "站点较慢" in js)
        check("加载失败给 toast, 不是无声",
              "toast('加载失败: ' + e.message)" in js)
        css = (ROOT / "server" / "static" / "css" / "app.css").read_text("utf-8")
        check("到底文案有样式(.mag-end)", ".mag-end" in css)
    finally:
        D.search = real_search

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
