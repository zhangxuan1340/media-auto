#!/usr/bin/env python3
"""media-auto / verify_track_filter —— 追踪「片源/分辨率/发布组」筛选回归
==============================================================================
背景(2026-09-28 用户需求): 追踪自动推磁力只按「4K/1080p + 大小区间」筛,
不够快也不够准 —— 要能按 **格式(BluRay / WEB-DL / WEBRip / REMUX…)、
分辨率(2160p/1080p/720p/480p)、发布组(SPARK/Sai/NTb…)** 收紧, 命中才推。

本脚本锁死:
  ① 分辨率: 像素写法走 mediainfo.resolution_rank, 裸 4K/UHD 走别名兜底;
     认不出 → **一律不推**(不猜);720p/480p 可选但没有大小区间(=不限大小);
  ② 分辨率档位「从未设置过」= 默认 2160p,1080p(= 上线前历史行为),
     显式清空 = 全部档;只认 2160p/1080p/720p/480p 四档;
  ③ 片源: WEBRip 必须与 WEB-DL **分开**(mediainfo.guess_source 把 webrip 并进
     WEBDL —— NFO 口径, 推送筛选要细分); 开了片源筛选而认不出 → 不推;
  ④ 发布组: 名字尾巴 -GROUP(与 naming.RELEASE_GROUP 同一口径, 剥掉扩展名再匹配),
     认不出 → 不推;匹配大小写不敏感;
  ⑤ 大小区间只对 2160p/1080p 生效, 命中边界(最小/最大)照常放行;
  ⑥ 四个维度是「与」关系: 全部命中才推;
  ⑦ normalize_setting_lists: 未知项丢弃、别名归一(web-dl→WEBDL、4k→2160p)、
     None=不改、空串=清成不限。

用法: ./venv/bin/python scripts/verify_track_filter.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* 之前指好库路径(db/database.py 导入时就建 engine)
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_trackfilter_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import SessionLocal, init_db          # noqa: E402
from db import repositories as repo                    # noqa: E402
from scripts import track_check as tc                  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


GB = 1024 ** 3


def S(res=("2160p", "1080p"), src=(), grp=(), size=None):
    """构造一份 get_track_settings 形状的筛选状态(便于直接测 _passes)。"""
    return {"auto_push": False,
            "resolutions": list(res), "sources": list(src), "groups": list(grp),
            "size": size or {"2160p": (None, None), "1080p": (None, None)}}


def main():
    init_db()

    print("\n-- 分辨率档 _resolution_class --")
    cases = [
        ("Napoleon.2023.2160p.BluRay.x265-SPARK", "2160p"),
        ("Some.Film.1080p.WEB-DL.H265", "1080p"),
        ("Some.Show.1080i.HDTV.x264", "1080p"),      # 1080i 归 1080p
        ("Some.Show.720p.WEBRip", "720p"),
        ("Movie.480p.DVDRip", "480p"),
        ("Movie.4K.HDR.SDR", "2160p"),                # 裸 4K 别名兜底
        ("Movie.UHD.BluRay", "2160p"),                # UHD 别名
        ("Movie.2019.4320p", "2160p"),                # 8K 归 2160p 档
    ]
    for name, want in cases:
        check(f"res {want} <- {name[:36]}", tc._resolution_class(name) == want,
              f"got={tc._resolution_class(name)!r}")
    for name in ("中字版", "Movie.1080x.BluRay", "Fight.Club.DD5.1"):
        check(f"res None <- {name}", tc._resolution_class(name) is None,
              f"got={tc._resolution_class(name)!r}")

    print("\n-- 片源 _source_of(区分 WEBRip / WEB-DL) --")
    for name, want in [
        ("Movie.2160p.Remux", "REMUX"),
        ("Movie.2160p.BluRay.x265", "BLURAY"),
        ("Movie.1080p.BDRip", "BLURAY"),
        ("Movie.2160p.WEB-DL.H265", "WEBDL"),
        ("Movie.1080p.WEBRip.H264", "WEBRIP"),
        ("Movie.1080p.HDTV", "HDTV"),
        ("Some.Show.DD5.1.x264", "NONE"),
    ]:
        got = tc._source_of(name)
        check(f"src {want} <- {name[:36]}", got == want, f"got={got!r}")

    print("\n-- 发布组 _group_of(尾巴 -GROUP, 先剥扩展名) --")
    for name, want in [
        ("Napoleon.2023.2160p.BluRay.x265-SPARK", "SPARK"),
        ("Some.Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", "NTb"),
        ("Some.Show.720p.WEBRip-Sai", "Sai"),
        ("Movie.2024.2160p.BluRay.x265", ""),
        ("Movie.2024.1080p.BluRay.x265-", ""),       # 尾巴不完整 → 认不出
    ]:
        got = tc._group_of(name)
        check(f"grp {want or '(空)'} <- {name[:36]}", got == want, f"got={got!r}")

    print("\n-- _passes: 分辨率 --")
    check("默认两档推 2160p",
          tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-SPARK", S()))
    check("默认两档不推 720p",
          not tc._passes(2 * GB, "Movie.2023.720p.WEBRip", S()))
    check("清空档位(不限)后 720p 可推",
          tc._passes(2 * GB, "Movie.2023.720p.WEBRip", S(res=())))
    check("清空档位也仍不推认不出的分辨率",
          not tc._passes(2 * GB, "Movie.2023.中字版", S(res=())))
    check("只选 1080p 时不推 2160p",
          not tc._passes(20 * GB, "Movie.2023.2160p.BluRay", S(res=("1080p",))))

    print("\n-- _passes: 片源 --")
    check("开片源=BLURAY 时推 BluRay",
          tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-SPARK", S(src=("BLURAY",))))
    check("开片源=BLURAY 时不推 WEB-DL",
          not tc._passes(8 * GB, "Movie.2023.2160p.WEB-DL.x265", S(src=("BLURAY",))))
    check("开片源=WEBDL 时推 WEB-DL 但不推 WEBRip",
          tc._passes(8 * GB, "Movie.2023.1080p.WEB-DL", S(src=("WEBDL",)))
          and not tc._passes(4 * GB, "Movie.2023.1080p.WEBRip", S(src=("WEBDL",))))
    check("开片源=WEBRIP 时只推 WEBRip",
          tc._passes(4 * GB, "Movie.2023.1080p.WEBRip", S(src=("WEBRIP",)))
          and not tc._passes(4 * GB, "Movie.2023.1080p.WEB-DL", S(src=("WEBRIP",))))
    check("开片源筛选而认不出 → 不推",
          not tc._passes(4 * GB, "Movie.2023.1080p.DD5.1", S(src=("BLURAY",))))
    check("不开片源筛选 → 不看片源",
          tc._passes(4 * GB, "Movie.2023.1080p.HDTV", S()))

    print("\n-- _passes: 发布组 --")
    check("推 SPARK 组",
          tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-SPARK", S(grp=("SPARK",))))
    check("大小写不敏感(spark 命中 SPARK)",
          tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-SPARK", S(grp=("spark",))))
    check("不推别的组(NTb)",
          not tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-NTb", S(grp=("SPARK",))))
    check("认不出组 → 不推",
          not tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265", S(grp=("SPARK",))))
    check("带扩展名也能认出组",
          tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-SPARK.mkv", S(grp=("SPARK",))))
    check("多组任一命中即可",
          tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-NTb", S(grp=("SPARK", "NTb"))))
    check("不开组筛选 → 不看组",
          tc._passes(20 * GB, "Movie.2023.2160p.BluRay.x265-SPARK", S(grp=())))

    print("\n-- _passes: 大小区间(只有 2160p/1080p 两档) --")
    sz = {"2160p": (20, 80), "1080p": (2, 8)}
    check("2160p 小于下限不推", not tc._passes(10 * GB, "Movie.2160p.BluRay", S(size=sz)))
    check("2160p 下限边界放行", tc._passes(20 * GB, "Movie.2160p.BluRay", S(size=sz)))
    check("2160p 上限边界放行", tc._passes(80 * GB, "Movie.2160p.BluRay", S(size=sz)))
    check("2160p 大于上限不推", not tc._passes(90 * GB, "Movie.2160p.BluRay", S(size=sz)))
    check("1080p 独立区间生效", tc._passes(5 * GB, "Movie.1080p.WEB-DL", S(size=sz)))
    check("720p 没有区间 = 不限大小",
          tc._passes(99 * GB, "Movie.720p.WEBRip", S(res=(), size=sz)))
    check("size 非法(无法转 float)不推",
          not tc._passes(None, "Movie.2160p.BluRay", S(size=sz)))

    print("\n-- _passes: 四维是「与」关系 --")
    st = S(res=("2160p",), src=("BLURAY",), grp=("SPARK",), size=sz)
    check("全命中 → 推",
          tc._passes(30 * GB, "Movie.2023.2160p.BluRay.x265-SPARK", st))
    check("片源不对 → 不推",
          not tc._passes(30 * GB, "Movie.2023.2160p.WEB-DL.x265-SPARK", st))
    check("组不对 → 不推",
          not tc._passes(30 * GB, "Movie.2023.2160p.BluRay.x265-NTb", st))
    check("大小不对 → 不推",
          not tc._passes(5 * GB, "Movie.2023.2160p.BluRay.x265-SPARK", st))

    print("\n-- normalize_setting_lists --")
    n = tc.normalize_setting_lists("2160p,9999p,720p", "webdl,bluray,WEB-RIP,xxx", "SPARK, sai ,SPARK")
    check("分辨率未知项丢弃", n["resolutions"] == "2160p,720p", n["resolutions"])
    check("片源别名归一", n["sources"] == "WEBDL,BLURAY,WEBRIP", n["sources"])
    check("组去空去重保序", n["groups"] == "SPARK,sai", n["groups"])
    check("None = 不改(三项都返回 None)",
          tc.normalize_setting_lists() == {"resolutions": None, "sources": None, "groups": None})
    check("空串 = 清成不限",
          tc.normalize_setting_lists("", "", "") ==
          {"resolutions": "", "sources": "", "groups": ""})
    check("分辨率别名 4k→2160p / 1080→1080p",
          tc.normalize_setting_lists("4k,1080", None, None)["resolutions"] == "2160p,1080p")

    print("\n-- get_track_settings 默认值 --")
    s = SessionLocal()
    try:
        st = tc.get_track_settings(s)
        check("从未设置 → 默认两档 2160p,1080p", st["resolutions"] == ["2160p", "1080p"],
              st["resolutions"])
        check("从未设置 → 片源/组为空(不限)",
              st["sources"] == [] and st["groups"] == [])
        check("size 按分辨率档给(2160p/1080p)",
              set(st["size"]) == {"2160p", "1080p"} and st["size"]["2160p"] == st["size_4k"])
        check("默认无大小区间 = 两个都是 (None, None)",
              st["size_4k"] == (None, None) and st["size_1080"] == (None, None))
        # 显式清空 → 全部档(不再是默认两档)
        repo.set_setting(s, tc.KEY_RESOLUTIONS, "")
        repo.set_setting(s, tc.KEY_SOURCES, "BLURAY,WEBRIP")
        repo.set_setting(s, tc.KEY_GROUPS, "spark")
        s.commit()
        st = tc.get_track_settings(s)
        check("显式清空 → 分辨率不限", st["resolutions"] == [], st["resolutions"])
        check("设置落库回读", st["sources"] == ["BLURAY", "WEBRIP"] and st["groups"] == ["spark"],
              st)
        check("_rule_summary 可读", "BLURAY" in tc._rule_summary(st), tc._rule_summary(st))
    finally:
        s.close()

    print("\n-- /api/track/settings 语义(传了才改 / 空串=不限 / 未知项丢弃) --")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from server.routers.track import router
        from server.auth import require_auth

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: None
        client = TestClient(app)
        repo.set_setting(s, tc.KEY_RESOLUTIONS, "")
        repo.set_setting(s, tc.KEY_SOURCES, "")
        repo.set_setting(s, tc.KEY_GROUPS, "")
        s.commit()
        st = client.get("/api/track/settings").json()
        check("GET 清空后三项都为空(不限)", st["resolutions"] == [] and st["sources"] == []
              and st["groups"] == [], st)
        st = client.post("/api/track/settings?resolutions=2160p,720p"
                         "&sources=webdl,xxx&groups=SPARK, sai ,SPARK").json()
        check("POST 规范化: 分辨率/片源别名归一 + 未知项丢弃 + 组去重",
              st["resolutions"] == ["2160p", "720p"] and st["sources"] == ["WEBDL"]
              and st["groups"] == ["SPARK", "sai"], st)
        st = client.post("/api/track/settings?auto_push=false").json()
        check("不传筛选项 = 保持原值", st["resolutions"] == ["2160p", "720p"]
              and st["groups"] == ["SPARK", "sai"], st)
        st = client.post("/api/track/settings?resolutions=&sources=&groups=").json()
        check("空串 = 清成不限", st["resolutions"] == [] and st["sources"] == []
              and st["groups"] == [], st)
    except Exception as e:  # noqa: BLE001
        check("settings API 走通", False, str(e)[:200])

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
