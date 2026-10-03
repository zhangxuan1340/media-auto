#!/usr/bin/env python3
"""media-auto / verify_group_rank —— 种子抓取规则(前排发布组)回归
==============================================================================
背景(2026-09-28 用户需求): FRDS / Beitai / 各大 PT 站的组要「前排」——
详情页种子搜索的「质量优先」排序整批排最前, 追踪自动推送也按同一规则挑。

本脚本锁死:
  ① 列表口径: 配置键缺失 → 内置默认名单(含 FRDS/Beitai); 显式 `[]` → 关闭;
     传字符串兼容; 去空白 + 大小写去重(保留首次出现的写法);
  ② 命中口径: `-Beitai` / `[FRDS]` / 大小写不敏感都算; 组名两侧不紧挨字母数字
     (`CHDRip` 不算组 `CHD`、`SSDX` 不算组 `SSD`); 没命中 → None;
  ③ `quality` 排序: 前排组整批在前且**按配置顺序**, 组内/其余再按质量分 → 大小 → 种子数;
     关闭(`[]`)时退回纯质量序; `size_desc`/`seeders_desc` 不受前排影响;
  ④ 排序缓存 key 带规则指纹: 改前排组/金标组立即换 key(不等 120s TTL);
  ⑤ 回传字段: `groupRank` / `groupName`(quality 与 relevance 都带), quality 另带
     `qualityScore` / `golden`; 排序结果与 `_apply_sort` 同序;
  ⑥ 追踪 `_pick_best(cfg)`: 前排组压过种子数更多的非前排组; 关闭时仍是种子数优先;
  ⑦ 前后端同序: 把 detail.js 里的 `_magSortCmp` 抠出来跑同一份数据, 与后端排序结果比对。

用法: ./venv/bin/python scripts/verify_group_rank.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* 之前指好库路径(db/database.py 导入时就建 engine)
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_grouprank_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import init_db                      # noqa: E402
from server.routers import search as s                # noqa: E402
from scripts import track_check as tc                 # noqa: E402

FAILED = []
GB = 1024 ** 3
CFG = {"search": {"group_priority": ["FRDS", "Beitai", "CHD", "SSD"]}}
CFG_OFF = {"search": {"group_priority": []}}

def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def R(name, size, seeders=None):
    return {"name": name, "size": size, "seeders": seeders}


N_ZZZ = "Zzz.2024.2160p.HDR.x265.7.1-Other"
N_YY = "Yyy.2024.1080p.WEB-DL.x264-Beitai"
N_XX = "Xxx.2024.480p.HDTV.x264-Other"
N_WW = "Www.2024.1080p.BluRay.x264-FRDS"


def sample_items():
    return [R(N_ZZZ, 20 * GB, 100), R(N_YY, 8 * GB, 5),
            R(N_XX, 1 * GB, 10), R(N_WW, 6 * GB, 20)]


def _extract_cmp_src():
    """把 detail.js 里的 `function _magSortCmp(...)` 整段抠出来(花括号配平扫描)。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "server", "static", "js", "detail.js")
    src = open(path, encoding="utf-8").read()
    start = src.index("function _magSortCmp")
    depth, i = 0, src.index("{", start)
    end = i
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
        i += 1
    return src[start:end + 1]


def _frontend_order(items, sort="quality"):
    """用 node 跑 detail.js 的 _magSortCmp 对同一份 items 排序, 返回名字序列。"""
    script = (_extract_cmp_src() + "\nconst items = " + json.dumps(items, ensure_ascii=False)
              + ";\nitems.sort(_magSortCmp(" + json.dumps(sort) + "));\n"
              + "console.log(JSON.stringify(items.map(x => x.name)));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout)[:400])
    return json.loads(out.stdout.strip().splitlines()[-1])


def main():
    init_db()

    print("\n-- ① 列表口径 --")
    lst = s.group_priority_list({})
    check("键缺失 → 内置默认名单, 且含 FRDS/Beitai",
          "FRDS" in lst and "Beitai" in lst and len(lst) >= 5, lst)
    check("显式空数组 → 关闭(返回空)", s.group_priority_list(CFG_OFF) == [])
    check("传字符串也兼容", s.group_priority_list({"search": {"group_priority": "frds"}}) == ["frds"])
    check("去空白 + 大小写去重(保留首次写法)",
          s.group_priority_list({"search": {"group_priority": [" frds ", "FRDS", "Beitai", ""]}})
          == ["frds", "Beitai"])

    print("\n-- ② 命中口径 --")
    check("尾巴 -BeiTai 命中(Beitai 在第 2 位)",
          s.group_rank("X.2024.1080p.WEB-DL.H.264-BeiTai", CFG) == 1)
    check("前缀 [FRDS] 命中", s.group_rank("[FRDS]Some.2024.1080p.WEB", CFG) == 0)
    check("大小写不敏感",
          s.group_rank("Movie.2024.MA.WEB-CHD", {"search": {"group_priority": ["FRDS", "Beitai", "chd"]}}) == 2)
    check("CHDRip 不算组 CHD", s.group_rank("Movie.2024.CHDRip.x264", CFG) is None)
    check("SSDX 不算组 SSD", s.group_rank("Movie.2024.SSDX.x264", CFG) is None)
    check("没配置的组 → None", s.group_rank("Movie.2024-UnknownGRP", CFG) is None)
    check("空名 → None", s.group_rank("", CFG) is None)
    check("关闭时一律 None", s.group_rank("Movie-BeiTai", CFG_OFF) is None)

    print("\n-- ③ quality 排序: 前排组整批在前(按配置顺序) --")
    got = [r["name"] for r in s._apply_sort(sample_items(), "quality", CFG)]
    want = [N_WW, N_YY, N_ZZZ, N_XX]
    check("FRDS > Beitai > 其余按质量(前排压过 2160p 非前排)", got == want, got)
    got0 = [r["name"] for r in s._apply_sort(sample_items(), "quality", CFG_OFF)]
    want0 = [N_ZZZ, N_WW, N_YY, N_XX]
    check("关闭前排 → 退回纯质量序", got0 == want0, got0)
    sizes = [r["name"] for r in s._apply_sort(sample_items(), "size_desc", CFG)]
    check("size_desc 不受前排影响(按大小降序)", sizes == [N_ZZZ, N_YY, N_WW, N_XX], sizes)
    seeds = [r["name"] for r in s._apply_sort(sample_items(), "seeders_desc", CFG)]
    check("seeders_desc 不受前排影响(种子多在前)", seeds[0] == N_ZZZ, seeds)

    print("\n-- ④ 缓存 key 带规则指纹 --")
    k1 = s._sort_key("q", "quality", "next_web", CFG)
    k2 = s._sort_key("q", "quality", "next_web", {"search": {"group_priority": ["SPARK"]}})
    k3 = s._sort_key("q", "quality", "next_web", CFG_OFF)
    check("前排组不同 → key 不同", k1 != k2, (k1, k2))
    check("关闭前排 → key 也不同", k1 != k3, (k1, k3))
    check("金标组变化也换 key",
          k1 != s._sort_key("q", "quality", "next_web",
                            {"search": {"group_priority": CFG["search"]["group_priority"],
                                        "golden_groups": ["FRDS"]}}))
    s._cache_put(k1, [{"name": "stale"}])
    check("改配置后旧缓存不再命中(新 key miss)", s._cache_get(k2) is None)
    s._sort_cache.clear()

    print("\n-- ⑤ /api/search 回传字段与顺序 --")
    payload = [
        {"name": N_WW, "size": 6 * GB, "seeders": 20, "infoHash": "a" * 40,
         "magnet": "magnet:?xt=urn:btih:" + "a" * 40},
        {"name": N_ZZZ, "size": 20 * GB, "seeders": 100, "infoHash": "b" * 40,
         "magnet": "magnet:?xt=urn:btih:" + "b" * 40},
    ]
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from server.auth import require_auth

        async def fake_fetch_all(source, cfg, q, cap, *a, **k):
            return [dict(x) for x in payload]

        async def fake_search_next_web(cfg, q, limit, page=1):
            return [dict(x) for x in payload], False, 1

        real_fetch, real_nw = s._fetch_all, s._search_next_web
        s._fetch_all, s._search_next_web = fake_fetch_all, fake_search_next_web
        try:
            app = FastAPI()
            app.include_router(s.router)
            app.dependency_overrides[require_auth] = lambda: None
            client = TestClient(app)

            r = client.get("/api/search?q=test&sort=quality&limit=10").json()
            its = r["items"]
            check("quality 首条 = 前排组(FRDS) 且带 groupName",
                  bool(its) and its[0].get("groupRank") == 0
                  and its[0].get("groupName") == "FRDS", its[:1])
            check("quality 回传 qualityScore / golden",
                  "qualityScore" in its[0] and "golden" in its[0], its[0])
            check("非前排组 groupRank/groupName = None",
                  its[1].get("groupRank") is None and its[1].get("groupName") is None, its[1])
            expect = [x["name"] for x in s._apply_sort([dict(x) for x in payload],
                                                       "quality", s.get_config())]
            check("API 顺序 = _apply_sort 顺序", [x["name"] for x in its] == expect,
                  ([x["name"] for x in its], expect))

            r2 = client.get("/api/search?q=test&sort=relevance&limit=10").json()
            check("relevance 也带 groupRank / groupName",
                  all("groupRank" in x and "groupName" in x for x in r2["items"]), r2["items"])
            check("relevance 不带 qualityScore(该排序不算质量分)",
                  all("qualityScore" not in x for x in r2["items"]), r2["items"])
        finally:
            s._fetch_all, s._search_next_web = real_fetch, real_nw
            s._sort_cache.clear()
    except Exception as e:  # noqa: BLE001
        check("search API 走通", False, str(e)[:300])

    print("\n-- ⑥ 追踪 _pick_best 按同一规则挑 --")
    a = R(N_ZZZ, 20 * GB, 80)
    b = R(N_YY, 8 * GB, 3)
    check("前排组压过种子数更多的非前排", tc._pick_best([a, b], CFG)["name"] == N_YY,
          tc._pick_best([a, b], CFG))
    check("关闭前排 → 仍是种子数优先",
          tc._pick_best([a, b], CFG_OFF)["name"] == N_ZZZ)
    check("_pick_best 空列表 → None", tc._pick_best([], CFG) is None)

    print("\n-- ⑦ 前端 _magSortCmp 与后端同序 --")
    try:
        base = sample_items()

        def with_rank(cfg):
            out = []
            for x in base:
                y = dict(x)
                y["groupRank"] = s.group_rank(x["name"], cfg)
                y["qualityScore"] = s.quality_score(x["name"], cfg, x.get("size"))
                out.append(y)
            return out

        be = [x["name"] for x in s._apply_sort(with_rank(CFG), "quality", CFG)]
        fe = _frontend_order(with_rank(CFG))
        check("前排开启: 前端合并重排 = 后端排序", fe == be, f"前端 {fe} 后端 {be}")
        be0 = [x["name"] for x in s._apply_sort(with_rank(CFG_OFF), "quality", CFG_OFF)]
        fe0 = _frontend_order(with_rank(CFG_OFF))
        check("前排关闭: 前端合并重排 = 后端排序", fe0 == be0, f"前端 {fe0} 后端 {be0}")
    except Exception as e:  # noqa: BLE001
        check("前端比较器同序", False, str(e)[:300])

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
