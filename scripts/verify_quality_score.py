#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【纯本地·打桩】质量分回归: 分辨率写实 + 体积合理性

报障(2026-09-30): 详情页「质量优先」里「明明很多低质量的变成了高质量」——
`痴迷(2026)【4K.SDR1080p】` 这种名字按 "4K" 拿满 48 分压过真 1080p 蓝光;
一批名字吹 4K/REMUX、体积只有几十 MB~1.2G 的(短片/拼接/.exe 诱饵)也排在真原盘前面。

修法(都在 server/routers/search.py):
  ① resolution(): 出现 2160P/3840x2160 才算 uhd; 只写 4K/UHD 但同时写了 1080 → 按 1080 算;
  ② _size_adjust(): 按分辨率给"该有的体积"打分, 名不副实扣分(size <1MiB 视为站点占位, 不奖不罚);
  ③ _annotate_suspect(): 回传 sizeSuspect, 前端 detail.js::_qualityTags 出「体积可疑」标签
     (分辨率标签也改成与 ① 同规则, 否则界面照样显示 2160p 骗人)。

用法: ./venv/bin/python scripts/verify_quality_score.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* 之前指好库路径(db/database.py 导入时就建 engine)
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_quality_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import init_db                      # noqa: E402
from server.routers import search as s                # noqa: E402

FAILED = []
GB = 1024 ** 3
MB = 1024 ** 2
CFG = {"search": {"group_priority": []},
       "bitmagnet_next_web": {"enabled": True, "base": "http://fake"},
       "bitmagnet": {"enabled": False}}

# 真实样本(2026-09-30 现场抓的种子名 + 体积)
N_UHD_BIG = "Obsession 2026 2160p UHD BluRay DV P7 HDR TrueHD Atmos 7.1 x265 - RMXG"
N_UHD_MID = "Obsession (2026) (2160p UHD BluRay AV1 HDR Sepiol).mkv"
N_FAKE_EXE = "Obsession (2026) [2160p HD AMZN WEB-DL H264 ENG MULTISUB] - BTM.exe"
N_4K_TINY = "【yiyidj.org】痴迷(2026)【4K.REMUXUHD原盘】【HDR10&杜比视界】【中文字幕】【悬疑恐怖】"
N_4K_1080 = "【yiyidj.org】痴迷(2026)【4K.SDR1080p】【高码率】【外挂内嵌简英双语字幕】【悬疑恐怖】"
N_4K_WEB = "【yiyidj.org】痴迷Obsession(2026)[WEB-4K][外挂中文字幕]"
N_FHD_BDRAY = "痴迷.2026.HD1080P.AAC.H264.CHS-ENG.BTSJ6"
N_4K_MARK = "【8i2.org】痴迷Obsession(2026)[4K蓝光][DV&HDR][内封中英双语字幕]"


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def _extract_js(func):
    """把 detail.js 里的 `function <func>(...)` 整段抠出来(花括号配平扫描)。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "server", "static", "js", "detail.js")
    src = open(path, encoding="utf-8").read()
    start = src.index(f"function {func}")
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


def _tags_html(name, r=None):
    """用 node 跑 detail.js 的 _qualityTags, 返回渲染出来的 HTML 片段。"""
    script = (_extract_js("_qualityTags")
              + "\nconst name = " + json.dumps(name, ensure_ascii=False)
              + ";\nconst r = " + json.dumps(r, ensure_ascii=False)
              + ";\nconsole.log(_qualityTags(name, r));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout)[:400])
    return out.stdout.strip().splitlines()[-1]


def main():
    init_db()

    print("\n-- ① 分辨率写实: 2160/4K/1080 冲突怎么判 --")
    check("2160p → uhd", s.resolution(N_UHD_BIG) == "uhd", s.resolution(N_UHD_BIG))
    check("2160p UHD BluRay → uhd", s.resolution(N_UHD_MID) == "uhd", s.resolution(N_UHD_MID))
    check("4K蓝光(没写 1080) → uhd", s.resolution(N_4K_MARK) == "uhd", s.resolution(N_4K_MARK))
    check("WEB-4K(没写 1080) → uhd", s.resolution(N_4K_WEB) == "uhd", s.resolution(N_4K_WEB))
    check("【4K.SDR1080p】 → fhd(不是 uhd)",
          s.resolution(N_4K_1080) == "fhd", s.resolution(N_4K_1080))
    check("HD1080P → fhd", s.resolution(N_FHD_BDRAY) == "fhd", s.resolution(N_FHD_BDRAY))
    check("720p → hd", s.resolution("Movie.2024.720p.WEB.x264") == "hd")
    check("480p → sd", s.resolution("Movie.2024.480p.WEB.x264") == "sd")
    check("没写分辨率 → None", s.resolution("Movie.2026.WEB.x264") is None)
    check("空名 → None", s.resolution(None) is None)

    print("\n-- ② 体积合理性: 同名不同体积, 分数要拉开 --")
    big = s.quality_score(N_UHD_BIG, CFG, 52 * GB)
    mid = s.quality_score(N_UHD_MID, CFG, 8 * GB)
    tiny = s.quality_score(N_4K_TINY, CFG, 68 * MB)
    exe = s.quality_score(N_FAKE_EXE, CFG, 1200 * MB)
    fhd = s.quality_score(N_FHD_BDRAY, CFG, 2 * GB)
    check(f"真 4K 52G({big}) > 真 4K 8G({mid}) > 真 1080p 2G({fhd})",
          big > mid > fhd, (big, mid, fhd))
    check(f"4K 68M({tiny}) 掉到 真 1080p 2G({fhd}) 之下", tiny < fhd, (tiny, fhd))
    check(f"4K 1.2G .exe({exe}) 掉到 真 1080p 2G({fhd}) 之下", exe < fhd, (exe, fhd))
    check("体积未知(size=None/0) 不奖不罚",
          s._size_adjust("uhd", None) == 0 and s._size_adjust("uhd", 0) == 0)
    check("体积未知 与 <1MiB 占位 同分",
          s.quality_score(N_UHD_BIG, CFG, None)
          == s.quality_score(N_UHD_BIG, CFG, 120 * 1024))
    check("<1MiB 的占位体积不罚(站点只回 ~100KB 的条目)",
          s._size_adjust("uhd", 120 * 1024) == 0, s._size_adjust("uhd", 120 * 1024))
    check("4K 1~4G 罚 -45", s._size_adjust("uhd", 2 * GB) == -45, s._size_adjust("uhd", 2 * GB))
    check("4K 100M~1G 罚 -60", s._size_adjust("uhd", 300 * MB) == -60, s._size_adjust("uhd", 300 * MB))
    check("4K <100M 罚 -75", s._size_adjust("uhd", 50 * MB) == -75, s._size_adjust("uhd", 50 * MB))
    check("4K ≥10G 加 +6, ≥30G 加 +10",
          s._size_adjust("uhd", 12 * GB) == 6 and s._size_adjust("uhd", 60 * GB) == 10)
    check("1080p 1.5~6G 不罚, <400M 罚 -35",
          s._size_adjust("fhd", 3 * GB) == 0 and s._size_adjust("fhd", 200 * MB) == -35)

    print("\n-- ③ 老规则不回归(金标/样片/前排组仍在) --")
    bare = "长津湖.2021.2160p.WEB-DL.x265"
    gold = "长津湖.2021.2160p.WEB-DL.国语中字-x265"
    diff = s.quality_score(gold, CFG, 40 * GB) - s.quality_score(bare, CFG, 40 * GB)
    check("金标(中字+国语) 比同条件非金标 +32(中字6 + 国语6 + 金标20)", diff == 32, diff)
    check("占位体积不影响分数(与 size=None 同分)",
          s.quality_score(gold, CFG, 120 * 1024) == s.quality_score(gold, CFG, None))
    check("样本 -20 仍生效",
          s.quality_score("Movie.2026.SAMPLE.1080p.x264", CFG, 5 * MB)
          < s.quality_score("Movie.2026.1080p.x264", CFG, 5 * MB))

    print("\n-- ④ _apply_sort 顺序(带体积) --")
    items = [
        {"name": N_4K_TINY, "size": 68 * MB, "seeders": 999},
        {"name": N_UHD_MID, "size": 8 * GB, "seeders": 5},
        {"name": N_FHD_BDRAY, "size": 2 * GB, "seeders": 50},
        {"name": N_FAKE_EXE, "size": 1200 * MB, "seeders": 888},
        {"name": N_UHD_BIG, "size": 52 * GB, "seeders": 1},
    ]
    got = [x["name"] for x in s._apply_sort(list(items), "quality", CFG)]
    check("真 4K 52G 排第一", got[0] == N_UHD_BIG, got)
    check("真 4K 8G 排第二(体积次大)", got[1] == N_UHD_MID, got)
    check("68M 的假 4K 掉到 真 1080p 2G 之后",
          got.index(N_4K_TINY) > got.index(N_FHD_BDRAY), got)
    check("1.2G 的 2160p .exe 掉到 真 1080p 2G 之后",
          got.index(N_FAKE_EXE) > got.index(N_FHD_BDRAY), got)
    check("种子数 999 不能把 68M 的假 4K 抬过真种子(种子数只是最后一档)",
          got.index(N_4K_TINY) > got.index(N_UHD_MID), got)

    print("\n-- ⑤ API 回传: qualityScore 带体积口径 + sizeSuspect --")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from server.auth import require_auth
        from server.config import get_config

        real_fetch, real_nw = s._fetch_all, s._search_next_web

        async def fake_fetch(source, cfg, q, cap, *a, **k):
            return [dict(x, infoHash="%040x" % i) for i, x in enumerate(items)]

        async def fake_nw(cfg, q, limit, page=1):
            off = (page - 1) * limit
            chunk = [dict(x, infoHash="%040x" % i) for i, x in enumerate(items)][off:off + limit]
            return chunk, off + limit < len(items), page + 1

        s._fetch_all, s._search_next_web = fake_fetch, fake_nw
        app = FastAPI()
        app.include_router(s.router)
        app.dependency_overrides[require_auth] = lambda: None
        app.dependency_overrides[get_config] = lambda: CFG
        client = TestClient(app)
        try:
            d = client.get("/api/search?q=test&sort=quality&limit=10").json()
            by = {x["name"]: x for x in d["items"]}
            check("quality 分支回传 qualityScore 与 quality_score 同值",
                  by[N_UHD_BIG]["qualityScore"] == s.quality_score(N_UHD_BIG, CFG, 52 * GB),
                  by[N_UHD_BIG]["qualityScore"])
            check("quality 分支标记 sizeSuspect(68M 的 4K)",
                  by[N_4K_TINY].get("sizeSuspect") is True, by[N_4K_TINY].get("sizeSuspect"))
            check("真 4K 52G 不标 sizeSuspect",
                  by[N_UHD_BIG].get("sizeSuspect") is False, by[N_UHD_BIG].get("sizeSuspect"))
            check("1.2G 的 2160p .exe 标 sizeSuspect",
                  by[N_FAKE_EXE].get("sizeSuspect") is True, by[N_FAKE_EXE].get("sizeSuspect"))
            check("API 顺序 = 真 4K 在前", [x["name"] for x in d["items"]][0] == N_UHD_BIG,
                  [x["name"] for x in d["items"]])

            r = client.get("/api/search?q=test&sort=relevance&limit=10").json()
            rby = {x["name"]: x for x in r["items"]}
            check("relevance 也回传 sizeSuspect(不排分但要标可疑)",
                  rby[N_4K_TINY].get("sizeSuspect") is True, rby[N_4K_TINY].get("sizeSuspect"))
            check("relevance 仍然不回传 qualityScore",
                  all("qualityScore" not in x for x in r["items"]), r["items"])
        finally:
            s._fetch_all, s._search_next_web = real_fetch, real_nw
            s._sort_cache.clear()
    except Exception as e:  # noqa: BLE001
        check("search API 走通", False, str(e)[:300])

    print("\n-- ⑥ 前端标签与后端同口径 --")
    try:
        html = _tags_html(N_UHD_BIG, {"sizeSuspect": False})
        check("真 4K → 2160p 标签", 'qtag q-uhd' in html, html)
        html = _tags_html(N_4K_1080, {"sizeSuspect": False})
        check("【4K.SDR1080p】 → 1080p 标签(不显示 2160p)",
              'qtag q-fhd' in html and 'q-uhd' not in html, html)
        html = _tags_html(N_UHD_BIG, {"sizeSuspect": True})
        check("sizeSuspect → 「体积可疑」标签", '体积可疑' in html and 'q-bad' in html, html)
        html = _tags_html(N_UHD_BIG, {"sizeSuspect": False})
        check("不可疑时不出现「体积可疑」", '体积可疑' not in html, html)
        # 后端 resolution() 与前端 chip 的判定逐条对拍
        for nm in (N_UHD_BIG, N_UHD_MID, N_4K_MARK, N_4K_WEB, N_4K_1080, N_FHD_BDRAY,
                   "Movie.2024.720p.WEB", "Movie.2024.480p.WEB"):
            res = s.resolution(nm)
            cls = {"uhd": "q-uhd", "fhd": "q-fhd", "hd": "q-hd", "sd": "q-sd"}.get(res)
            out = _tags_html(nm, None)
            check(f"标签同口径: {nm[:34]!r} → {res}",
                  (cls in out) if cls else ('q-uhd' not in out and 'q-fhd' not in out
                                            and 'q-hd' not in out and 'q-sd' not in out), out)
        css = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "server", "static", "css", "app.css"), encoding="utf-8").read()
        check("app.css 有 .q-bad 样式", ".qtag.q-bad" in css)
    except Exception as e:  # noqa: BLE001
        check("前端标签对拍", False, str(e)[:300])

    print("\n-- ⑦ 降权发布组(2026-10-04): 无中文字幕的组(如 BTM/俄语)质量分 -20 压后 --")
    CFG_DEM = dict(CFG)
    CFG_DEM = {"search": {**CFG["search"], "group_demote": ["BTM", "RUS"]},
               "bitmagnet_next_web": CFG["bitmagnet_next_web"],
               "bitmagnet": CFG["bitmagnet"]}
    base = "Movie.2026.1080p.x265"
    dem = "Movie.2026.1080p.BTM.x265"
    check("BTM 组种子比同规格普通种子 -20",
          s.quality_score(dem, CFG_DEM) - s.quality_score(base, CFG_DEM) == -20,
          (s.quality_score(dem, CFG_DEM), s.quality_score(base, CFG_DEM)))
    check("整词匹配: BTMX 不算 BTM", s.demote_by("Movie.BTMX.1080p", CFG_DEM) is None)
    check("-BTM 命中 BTM", s.demote_by("Movie.2026.1080p-BTM", CFG_DEM) == "BTM")
    check("[RUS] 命中 RUS", s.demote_by("Movie.2026.[RUS].1080p", CFG_DEM) == "RUS")
    check("大小写不敏感(小写 btm 命中)", s.demote_by("movie.2026.btm.1080p", CFG_DEM) == "BTM")
    check("键缺失不降(向后兼容)",
          s.group_demote_list({}) == [] and s.demote_by("Movie.BTM.1080p", {}) is None)
    check("显式空数组 = 不降", s.group_demote_list({"search": {"group_demote": []}}) == [])
    check("组名列表去空白+去重(大小写不敏感)",
          s.group_demote_list({"search": {"group_demote": [" BTM ", "btm", "RUS", ""]}})
          == ["BTM", "RUS"])
    check("字符串配置兼容", s.group_demote_list({"search": {"group_demote": "BTM"}}) == ["BTM"])
    # 排序: 同规格下 BTM 压到普通种子之后(即便 BTM 种子数更多也压)
    sitems = [
        {"name": "Movie.2026.1080p.GOOD.x265", "size": 2 * GB, "seeders": 5},
        {"name": "Movie.2026.1080p.BTM.x265", "size": 2 * GB, "seeders": 999},
    ]
    sgot = [x["name"] for x in s._apply_sort(list(sitems), "quality", CFG_DEM)]
    check("质量优先下 BTM(种子999)压到 GOOD 之后", sgot[0] == "Movie.2026.1080p.GOOD.x265", sgot)
    # API 回传 demoted/demotedBy
    it = {"name": "Movie.2026.1080p.BTM"}
    s._annotate_quality([it], CFG_DEM)
    check("_annotate_quality 标 demoted+demotedBy", it["demoted"] is True and it["demotedBy"] == "BTM", it)
    it2 = {"name": "Movie.2026.1080p.GOOD"}
    s._annotate_quality([it2], CFG_DEM)
    check("非降权组 demoted=False", it2["demoted"] is False and it2["demotedBy"] is None, it2)
    # 指纹: 改 group_demote 要换 key(不等 120s 缓存)
    fp1 = s._rule_fingerprint(CFG)
    fp2 = s._rule_fingerprint(CFG_DEM)
    check("改 group_demote 换指纹(缓存失效)", fp1 != fp2, (fp1, fp2))

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
