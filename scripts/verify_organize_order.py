#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""【纯本地】整理逻辑顺序回归 —— 2026-09-29 审查发现的 6 处偏差逐条锁死。

本脚本不连 CD2 / TMDB / 豆瓣(cd2 与 resolve_full_meta 全部打桩), 只验证"顺序对不对":

  ① 预览只读: GET /api/organize/files 调 build_plans 必须 read_only=True
     (原来默认 False → 打开整理页就写 title/title_checked、消耗豆瓣一次性核对标记);
  ② clean_media_names 剥推广块后必须回写 plan["subtitles"]
     (原来只更新 media 的 dict → 字幕名带推广块时, 后面 payload 搬运/字幕跟随拿的是
      改名后已不存在的旧路径);
  ③ upgrade(升级版)不再是死状态: CLI run() 执行它(只清广告)并进汇总;
     Web 任务过滤器放行它; CLI 汇总里"共 N 个条目"与分类之和对得上;
  ④ 执行上限不再静默截断: 默认 100(原 20), 超限必须给出 job.note 提示;
  ⑤ 源 NFO 的删除发生在"确认要搬"之后: 目标冲突跳过时广告照删、旧 NFO 一个不动
     (原来删 NFO 早于冲突判定 → 跳过/搬运失败时条目没 NFO);补季分支同款守卫,
     且补季的源 NFO 还必须等"文件真的搬走了"才删(原来跟广告一起在开头删);
  ⑥ --json 与 --apply 同用直接报错(原来 --apply --json 静默只打印不执行),
     以及季号推断的 base_dir 透传(与 _mark_dedup 收到的一致)。

用法: ./venv/bin/python scripts/verify_organize_order.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 必须在 import db.* 之前指好库路径(网络任务会往 organize_logs 落库)
_TMPDIR = tempfile.mkdtemp(prefix="media_auto_orgorder_")
os.environ["MEDIA_AUTO_DB"] = os.path.join(_TMPDIR, "verify.db")

from db.database import init_db                    # noqa: E402
from scripts import organize as og                 # noqa: E402
from clients.clouddrive import client as cd2c      # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def CFG(**org):
    o = {"probe_media": False, "clean_media_names": True, "write_nfo": True}
    o.update(org)
    return {"organize": o, "tmdb": {"api_key": ""}}


def PLAN(status, name="条目 (2024)", kind="movie"):
    """run()/apply_plan 需要的最小 plan 形状。"""
    return {
        "source": "/Temp/" + name, "name": name, "is_dir": True,
        "status": status, "reason": "测试",
        "new_name": name, "target_root": "/Cloud/CnMovie",
        "target": "/Cloud/CnMovie/" + name, "existing": "/Cloud/CnMovie/" + name,
        "missing_seasons": [2] if status == "merge" else [],
        "ads": [], "junk": [], "nfos": [], "assets": [], "subtitles": [],
        "ad_files": [], "junk_files": [], "asset_files": [],
        "media": [{"name": name + ".mkv", "path": "/Temp/" + name + "/" + name + ".mkv",
                   "size": 1, "rel_dir": ""}],
        "media_count": 1, "media_bytes": 1, "ad_count": 0, "has_nfo": True,
        "meta": {"kind": kind, "title": name.split(" ")[0], "tmdb_id": 1},
        "category": "Cn", "category_folder": "CnMovie",
        "category_reasons": ["region=Cn"],
    }


class Patch:
    """把 og.cd2 / og.* 的函数换成桩, 退出时原样还原。"""

    def __init__(self, **kw):
        self.kw = kw
        self._old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            obj = og.cd2 if k.startswith("cd2.") else og
            attr = k.split(".", 1)[1] if k.startswith("cd2.") else k
            self._old[k] = getattr(obj, attr)
            setattr(obj, attr, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            obj = og.cd2 if k.startswith("cd2.") else og
            attr = k.split(".", 1)[1] if k.startswith("cd2.") else k
            setattr(obj, attr, v)
        return False


# ---------------------------------------------------------------------------
def case_preview_readonly():
    print("\n-- ① 预览只读(read_only=True) --")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.auth import require_auth
    import server.routers.cd2 as cd2api

    captured = {}

    def fake_build(cfg, base_dir=None, only=None, limit=None, read_only=False, **kw):
        captured["read_only"] = read_only
        return []

    real = og.build_plans
    og.build_plans = fake_build
    real_quota = cd2c.get_offline_quota
    real_recent = cd2c.list_offline_all
    cd2c.get_offline_quota = lambda *a, **k: {"total": 0, "used": 0, "left": 0}
    cd2c.list_offline_all = lambda *a, **k: []
    try:
        app = FastAPI()
        app.include_router(cd2api.router)
        app.dependency_overrides[require_auth] = lambda: None
        r = TestClient(app).get("/api/organize/files?limit=5")
        check("端点走通", r.status_code == 200, r.status_code)
        check("build_plans 收到 read_only=True", captured.get("read_only") is True, captured)
    except Exception as e:  # noqa: BLE001
        check("预览端点走通", False, str(e)[:300])
    finally:
        og.build_plans = real
        cd2c.get_offline_quota = real_quota
        cd2c.list_offline_all = real_recent


def case_clean_names_subtitle():
    print("\n-- ② clean_media_names 回写 plan['subtitles'] --")
    promo_video = "【高清影视之家发布 www.HDBTHD.com】误杀2[国语中字].Fireflies.mkv"
    promo_sub = "【高清影视之家发布 www.HDBTHD.com】误杀2[国语中字].Fireflies.chs.srt"
    plan = {
        "media": [{"name": promo_video, "path": "/Temp/x/" + promo_video, "size": 1,
                   "rel_dir": ""}],
        "subtitles": ["/Temp/x/" + promo_sub],
    }
    renamed = []

    def fake_rename(cfg, path, new_name, base_dir=None, **kw):
        renamed.append((path, new_name))

    with Patch(**{"cd2.rename_file": fake_rename,
                  "cd2.find_file_by_path": lambda *a, **k: None}):
        changed = og.clean_media_names(CFG(), plan, log=lambda *a: None)

    check("两个文件都剥了推广块", len(changed) == 2, changed)
    check("media 路径已更新", plan["media"][0]["name"].startswith("误杀2"),
          plan["media"][0]["name"])
    check("plan['subtitles'] 已回写成新路径",
          len(plan["subtitles"]) == 1
          and plan["subtitles"][0].endswith(".chs.srt")
          and plan["subtitles"][0] != "/Temp/x/" + promo_sub,
          plan["subtitles"])


def case_upgrade_cli():
    print("\n-- ③ upgrade 走 CLI: 会被执行(仅清广告)并进汇总 --")
    plans = [PLAN("ok", "A (2024)"), PLAN("upgrade", "B (2024)"),
             PLAN("duplicate", "C (2024)"), PLAN("merge", "D (2019)", kind="tv"),
             PLAN("unresolved", "E"), PLAN("no_media", "F")]
    calls = []

    def fake_apply(cfg, plan, base_dir=None, log=print):
        calls.append(plan["status"])
        return {"deleted": ["/Temp/x/ad.txt"], "cleaned_names": []}

    with Patch(**{"build_plans": lambda *a, **k: [dict(p) for p in plans],
                  "apply_plan": fake_apply}):
        summary = og.run(CFG(), apply=True, log=lambda *a, **k: None)

    check("ok/duplicate/merge/unresolved 都执行了",
          set(calls) >= {"ok", "duplicate", "merge", "unresolved"}, calls)
    check("upgrade 也执行了(不再被丢掉)", "upgrade" in calls, calls)
    check("汇总 upgrade 计划数 = 1", summary.get("upgrade") == 1, summary.get("upgrade"))
    check("汇总 upgrade_cleaned = 1", summary.get("upgrade_cleaned") == 1,
          summary.get("upgrade_cleaned"))
    total = summary["total"]
    parts = sum(summary.get(k, 0) for k in
                ("ok", "merge", "duplicate", "upgrade", "unresolved", "no_media"))
    check("分类之和 == total(汇总不再对不上)", parts == total, (parts, total))

    calls.clear()
    with Patch(**{"build_plans": lambda *a, **k: [dict(p) for p in plans],
                  "apply_plan": fake_apply}):
        dry = og.run(CFG(), apply=False, log=lambda *a, **k: None)
    check("dry-run 不执行任何条目", not calls, calls)
    check("dry-run 仍统计 upgrade 计划数", dry.get("upgrade") == 1, dry.get("upgrade"))


def case_api_limit_and_upgrade():
    print("\n-- ④ Web 执行: 默认上限 100 / 超限出 note / upgrade 放行 --")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.auth import require_auth
    import server.routers.cd2 as cd2api

    plans = [PLAN("upgrade", "UP (2024)")]
    plans += [PLAN("ok", f"Ok{i} (2024)") for i in range(119)]
    calls = []

    def fake_apply(cfg, plan, base_dir=None, log=lambda *a, **k: None):
        calls.append(plan["status"])
        return {"deleted": []}

    real_build, real_apply = og.build_plans, og.apply_plan
    og.build_plans = lambda *a, **k: [dict(p) for p in plans]
    og.apply_plan = fake_apply
    try:
        app = FastAPI()
        app.include_router(cd2api.router)
        app.dependency_overrides[require_auth] = lambda: None
        client = TestClient(app)

        def wait(job_id, timeout=10):
            end = time.time() + timeout
            while time.time() < end:
                s = client.get(f"/api/organize/apply/{job_id}").json()
                if s["status"] in ("done", "error"):
                    return s
                time.sleep(0.05)
            return s

        r = client.post("/api/organize/apply", json={"all": True})
        s = wait(r.json()["job_id"])
        check("任务完成且无报错", s["status"] == "done", s.get("error"))
        check("默认上限是 100(原 20)", s["count"] == 100, s["count"])
        check("截断必须出声(job.note)", "100" in (s.get("note") or ""), s.get("note"))
        check("upgrade 被选中执行(排在最前)",
              s["results"] and s["results"][0]["status"] == "upgrade",
              s["results"][:1])
        check("只跑了 100 条", len(s["results"]) == 100, len(s["results"]))

        calls.clear()
        r2 = client.post("/api/organize/apply",
                         json={"names": [p["name"] for p in plans[:8]], "limit": 8})
        s2 = wait(r2.json()["job_id"])
        check("显式 limit 按传的来", s2["count"] == 8, s2["count"])
        check("未超限不加 note", not (s2.get("note") or ""), s2.get("note"))
    except Exception as e:  # noqa: BLE001
        check("执行端点走通", False, str(e)[:300])
    finally:
        og.build_plans, og.apply_plan = real_build, real_apply


def case_nfo_after_conflict():
    print("\n-- ⑤ 源 NFO 只在'确认要搬'之后才删 --")
    deleted = []
    meta_calls = []

    def fake_find(cfg, parent, name, base_dir=None, **kw):
        return {"name": name, "isDirectory": True} if parent.startswith("/Cloud/") else None

    def fake_delete(cfg, paths, base_dir=None, **kw):
        deleted.append(list(paths))

    def fake_full(cfg, plan):
        meta_calls.append(1)
        return {"kind": "movie", "title": "条目", "tmdb_id": 1}

    plan = PLAN("ok")
    plan["ads"] = ["/Temp/条目 (2024)/ad.txt"]
    plan["nfos"] = ["/Temp/条目 (2024)/条目.mkv.nfo"]

    # A) 目标已存在 + on_conflict=skip → 广告删、旧 NFO 一个不动、resolve_full_meta 不被调用
    deleted.clear()
    meta_calls.clear()
    with Patch(**{"cd2.delete_files": fake_delete, "cd2.find_file_by_path": fake_find,
                  "resolve_full_meta": fake_full}):
        res = og.apply_plan(CFG(on_conflict="skip"), dict(plan), log=lambda *a: None)
    flat = [p for batch in deleted for p in batch]
    check("跳过分支: 广告删了", plan["ads"][0] in flat, flat)
    check("跳过分支: 源 NFO 保留(没进删除清单)", plan["nfos"][0] not in flat, flat)
    check("跳过分支: 连 resolve_full_meta 都不必调", not meta_calls, meta_calls)
    check("跳过分支: 结果带 skipped", bool(res.get("skipped")), res.get("skipped"))

    # B) 无冲突 → 确定要搬 → 源 NFO 被删(且只删一次)
    deleted.clear()
    meta_calls.clear()
    moved = {}

    def fake_no_find(cfg, parent, name, base_dir=None, **kw):
        return None

    with Patch(**{"cd2.delete_files": fake_delete,
                  "cd2.find_file_by_path": fake_no_find,
                  "resolve_full_meta": fake_full,
                  "cd2.rename_file": lambda *a, **k: None,
                  "cd2.move_file": lambda *a, **k: None,
                  "_organize_in_workspace": lambda *a, **k: moved,
                  "finalize_entry": lambda *a, **k: {"nfo": "/Cloud/x/tvshow.nfo"}}):
        res2 = og.apply_plan(CFG(), dict(plan), log=lambda *a: None)
    flat2 = [p for batch in deleted for p in batch]
    check("无冲突: 广告与源 NFO 都删了",
          plan["ads"][0] in flat2 and plan["nfos"][0] in flat2, flat2)
    check("无冲突: 发生了归位", bool(res2.get("moved")), res2.get("moved"))

    # C) 拿不到元数据 → 旧 NFO 保留(与 2026-09-26 P0 守卫一致)
    deleted.clear()

    def none_full(cfg, plan):
        return None

    with Patch(**{"cd2.delete_files": fake_delete,
                  "cd2.find_file_by_path": fake_no_find,
                  "resolve_full_meta": none_full,
                  "cd2.rename_file": lambda *a, **k: None,
                  "cd2.move_file": lambda *a, **k: None,
                  "_organize_in_workspace": lambda *a, **k: moved,
                  "finalize_entry": lambda *a, **k: {}}):
        og.apply_plan(CFG(), dict(plan), log=lambda *a: None)
    flat3 = [p for batch in deleted for p in batch]
    check("TMDB 取不到 → 源 NFO 保留, 广告照删",
          plan["nfos"][0] not in flat3 and plan["ads"][0] in flat3, flat3)

    # D) 补季分支同款守卫: resolve_full_meta=None → 不删源 NFO
    deleted.clear()
    mp = PLAN("merge", "Show (2019)", kind="tv")
    mp["ads"] = ["/Temp/Show (2019)/ad.txt"]
    mp["nfos"] = ["/Temp/Show (2019)/tvshow.nfo"]
    with Patch(**{"cd2.delete_files": fake_delete,
                  "cd2.get_subfiles": lambda *a, **k: [],
                  "cd2.ensure_folder": lambda cfg, p, n, base_dir=None, **k: p + "/" + n,
                  "scan_tree": lambda *a, **k: [],
                  "resolve_full_meta": none_full}):
        og._apply_merge_seasons(CFG(), dict(mp), log=lambda *a: None)
    flat4 = [p for batch in deleted for p in batch]
    check("补季: 广告删了", mp["ads"][0] in flat4, flat4)
    check("补季: 源 NFO 保留(TMDB 拿不到时)", mp["nfos"][0] not in flat4, flat4)

    # E) 补季: 源 NFO 必须排在【搬完之后】才删 —— 事件顺序 (广告删除 → 搬文件 → 删 NFO)
    deleted.clear()
    ev = []

    def rec_delete(cfg, paths, base_dir=None, **kw):
        deleted.append(list(paths))
        ev.append(("del", list(paths)))

    def rec_move(*a, **k):
        ev.append(("move", None))
        return None

    mp3 = PLAN("merge", "Show (2019)", kind="tv")
    mp3["ads"] = ["/Temp/Show (2019)/ad.txt"]
    mp3["nfos"] = ["/Temp/Show (2019)/tvshow.nfo"]

    def scan_s02(cfg, path, base_dir=None, **kw):
        return [{"name": "Show.S02E01.mkv",
                 "path": path.rstrip("/") + "/Show.S02E01.mkv", "size": 1, "rel_dir": ""}]

    with Patch(**{"cd2.delete_files": rec_delete,
                  "cd2.get_subfiles": lambda *a, **k: [],
                  "cd2.ensure_folder": lambda cfg, p, n, base_dir=None, **k: p + "/" + n,
                  "cd2.move_file": rec_move,
                  "cd2.rename_file": lambda *a, **k: None,
                  "scan_tree": scan_s02,
                  "resolve_full_meta": fake_full,
                  "_write_entry_nfo": lambda *a, **k: "/Cloud/CnTV/Show (2019)/tvshow.nfo"}):
        r5 = og._apply_merge_seasons(CFG(), dict(mp3), log=lambda *a: None)
    flat5 = [p for batch in deleted for p in batch]
    check("补季(真搬了): 源 NFO 被删", mp3["nfos"][0] in flat5, flat5)
    check("补季(真搬了): 广告也删了", mp3["ads"][0] in flat5, flat5)
    kinds = ["move" if k == "move" else ("nfo" if mp3["nfos"][0] in v else "other")
             for k, v in ev]
    check("补季(真搬了): NFO 删除发生在搬文件之后",
          "move" in kinds and kinds.index("nfo") > kinds.index("move")
          if "nfo" in kinds else False, kinds)
    check("补季(真搬了): 结果记为已并入", bool(r5.get("moved")), r5.get("moved"))

    # F) 补季: 没有可搬文件(已在库内/全 Skip)→ 源目录保留, 旧 NFO 必须留着
    deleted.clear()
    with Patch(**{"cd2.delete_files": fake_delete,
                  "cd2.get_subfiles": lambda *a, **k: [],
                  "cd2.ensure_folder": lambda cfg, p, n, base_dir=None, **k: p + "/" + n,
                  "scan_tree": lambda *a, **k: [],
                  "resolve_full_meta": fake_full,
                  "_write_entry_nfo": lambda *a, **k: "/Cloud/CnTV/Show (2019)/tvshow.nfo"}):
        r6 = og._apply_merge_seasons(CFG(), dict(mp), log=lambda *a: None)
    flat6 = [p for batch in deleted for p in batch]
    check("补季(没搬动): 源 NFO 保留", mp["nfos"][0] not in flat6, flat6)
    check("补季(没搬动): 广告照删", mp["ads"][0] in flat6, flat6)
    check("补季(没搬动): 结果带 skipped", bool(r6.get("skipped")), r6.get("skipped"))

    # G) 补季: 连库内目标都没有(提前 return)→ 源 NFO 不该在那之前被删
    deleted.clear()
    mp4 = PLAN("merge", "Show (2019)", kind="tv")
    mp4["existing"] = ""
    mp4["ads"] = ["/Temp/Show (2019)/ad.txt"]
    mp4["nfos"] = ["/Temp/Show (2019)/tvshow.nfo"]
    with Patch(**{"cd2.delete_files": fake_delete,
                  "resolve_full_meta": fake_full}):
        r7 = og._apply_merge_seasons(CFG(), dict(mp4), log=lambda *a: None)
    flat7 = [p for batch in deleted for p in batch]
    check("补季(缺目标提前返回): 源 NFO 保留", mp4["nfos"][0] not in flat7, flat7)
    check("补季(缺目标提前返回): 结果带 skipped", bool(r7.get("skipped")), r7.get("skipped"))


def case_cli_json_guard():
    print("\n-- ⑥ --json 与 --apply 互斥 + 季号推断带 base_dir --")
    old_argv = sys.argv
    sys.argv = ["organize.py", "--json", "--apply"]
    try:
        og.main()
        check("--json --apply 直接报错", False, "没有报错, 静默只打印")
    except SystemExit as e:
        check("--json --apply 直接报错(argparse exit 2)", e.code == 2, e.code)
    except Exception as e:  # noqa: BLE001
        check("--json --apply 直接报错", False, str(e)[:200])
    finally:
        sys.argv = old_argv

    seen = {}

    def fake_scan(cfg, path, base_dir=None, **kw):
        seen.setdefault("base_dir", base_dir)
        return [{"name": "Show.S01E01.mkv", "path": path + "/a.mkv", "size": 1, "rel_dir": ""}]

    # ⑥a: _offline_seasons 现复用 plan['media'] 提季, 不再二次 scan_tree 源目录(2026-10-03 性能)
    plan = PLAN("ok", "Show (2019)", kind="tv")
    plan["media"] = [{"name": "Show.S01E01.mkv", "path": "/Temp/Show (2019)/a.mkv",
                      "size": 1, "rel_dir": ""}]
    with Patch(**{"scan_tree": fake_scan}):
        got_seasons = og._offline_seasons(CFG(), plan, base_dir="/BASE")
    check("_offline_seasons 从 plan['media'] 提季(S01E01 → 1)", got_seasons == {1}, got_seasons)
    check("_offline_seasons 不再二次 scan_tree 源目录", "base_dir" not in seen, seen)

    seen.clear()
    with Patch(**{"scan_tree": fake_scan}):
        og._library_seasons(CFG(), "/Cloud/CnTV/Show (2019)", base_dir="/BASE")
    check("_library_seasons 把 base_dir 传给 scan_tree",
          seen.get("base_dir") == "/BASE", seen)


def main():
    init_db()
    case_preview_readonly()
    case_clean_names_subtitle()
    case_upgrade_cli()
    case_api_limit_and_upgrade()
    case_nfo_after_conflict()
    case_cli_json_guard()

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
