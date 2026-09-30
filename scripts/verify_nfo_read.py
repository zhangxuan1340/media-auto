#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【真 CD2】现有 NFO 读取的两条通道回归 + 更新闸门文案

报障(2026-09-30): 「更新 NFO」报 502「读不到现有 NFO 的内容(tvshow.nfo)…已拒绝覆盖」。
根因不是配置改了, 而是:
  - 拒覆盖闸门 2026-09-27 才加(commit 57a0846) —— 以前读不到也照写, 会抹掉
    <fileinfo>/<original_filename>/<source> 与观看状态, "能更新"是假象;
  - 读通道当时只有两条: 本地挂载(local_root) + WebDAV(受 account_root 限制,
    账号只开 /Temp 时对 /Cloud 结构性读不到)。

修复: 加了下载链接通道(gRPC GetDownloadUrlPath → HTTP GET, 只要 gRPC token,
不受 WebDAV 账号范围限制), 并把"哪条通道为什么失败"写进 502 文案。
2026-09-30 随后**全面下线 local_root**: 本地挂载通道删除, 读 NFO 只剩 WebDAV → 下载链接
两条 —— 本脚本同时守这条: 通道数、why 条数、502 文案里都不许再出现 local_root/本地挂载。

用法: ./venv/bin/python scripts/verify_nfo_read.py
退出码: 0 = 全部通过; 1 = 有失败项
"""
import copy
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def cloud_nfo_candidates(limit=5):
    """从本地库的 Jellyfin 路径里挑几个真实存在的剧集目录(要跑在有库的机器上)。"""
    db = os.environ.get("MEDIA_AUTO_DB") or str(ROOT / "data" / "media_auto.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "select path from jellyfin_item where type='Series' order by id limit 40").fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for (p,) in rows:
        d = p if not os.path.splitext(p)[1] else p.rsplit("/", 1)[0]
        out.append(d.rstrip("/") + "/tvshow.nfo")
        if len(out) >= limit:
            break
    return out


def main():
    from lib import config as lc
    from lib import mediainfo
    from clients.clouddrive import client as cd2
    from server.routers.nfo import _existing_nfo_text

    cfg = lc.load_config()
    folder = (cfg.get("clouddrive2") or {}).get("staging_dir") or "/Temp/.media_auto_stage"
    probe_path = folder + "/verify_nfo_read.txt"
    probe_folder, probe_name = folder, "verify_nfo_read.txt"
    marker = "<?xml version=\"1.0\"?><tvshow><!-- verify_nfo_read 2026-09-30 --></tvshow>"

    # ---------- 0) local_root 已全面下线(通道/配置/UI 三处都删干净) ----------
    check("0) mediainfo 不再暴露本地挂载 API(local_path/local_roots)",
          not hasattr(mediainfo, "local_path") and not hasattr(mediainfo, "local_roots"))
    settings_js = (ROOT / "server" / "static" / "js" / "settings.js").read_text("utf-8")
    check("0) 设置页不再有 local_root 字段", "clouddrive2.local_root" not in settings_js)
    check("0) config.example.json 不再有 local_root 键",
          '"local_root"' not in (ROOT / "config.example.json").read_text("utf-8"))

    # ---------- 1) 造一个 /Temp 下的探针文件(可删区, 不碰 /Cloud) ----------
    created = False
    try:
        cd2.ensure_folder(cfg, folder.rsplit("/", 1)[0], folder.rsplit("/", 1)[1])
        cd2.write_file(cfg, probe_path, marker)
        created = True
        print(f"  探针已写入 {probe_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 探针写入失败(跳过 WebDAV/下载通道的自建文件用例): {str(e)[:160]}")

    try:
        # ---------- 2) WebDAV 通道(account_root=/Temp 时 /Temp 在范围内) ----------
        if created:
            why = []
            txt = _existing_nfo_text(cfg, probe_folder, [probe_name], why=why)
            check("1) WebDAV 通道读到 /Temp 探针", txt == marker, f"why={why}")

        # ---------- 3) WebDAV 越界 → CD2 下载通道兜底 ----------
        only_cd2 = copy.deepcopy(cfg)
        only_cd2.setdefault("webdav", {})["account_root"] = "/Cloud"   # /Temp 不在范围内
        if created:
            why = []
            txt = _existing_nfo_text(only_cd2, probe_folder, [probe_name], why=why)
            check("2) WebDAV 越界时 CD2 下载通道读到探针", txt == marker, f"why={why}")

        # ---------- 4) 真实 /Cloud NFO(用户报障场景: 账号只开 /Temp) ----------
        nfo_paths = cloud_nfo_candidates()
        if not nfo_paths:
            print("  -  3) CD2 下载通道: 本地库里没有剧集路径, 跳过")
        else:
            ok = None
            for p in nfo_paths:
                folder_path, name = p.rsplit("/", 1)
                why = []
                txt = _existing_nfo_text(cfg, folder_path, [name], why=why)
                if txt:
                    ok = (p, len(txt), why)
                    break
            check("3) WebDAV 越界时, CD2 下载通道读到 /Cloud NFO",
                  ok is not None, "全部候选都读不到")
            if ok:
                print(f"       {ok[0]} → {ok[1]} 字节")

        # ---------- 5) 两条通道全断 → None, why 记满 2 条(供 502 文案) ----------
        import server.routers.nfo as nfo_mod
        orig_read = nfo_mod.cd2.read_file_text
        nfo_mod.cd2.read_file_text = lambda *a, **k: (None, "通道被测试关掉")
        try:
            broken = copy.deepcopy(cfg)
            broken.setdefault("webdav", {})["base"] = ""          # WebDAV 也掐掉
            why = []
            txt = _existing_nfo_text(broken, "/Cloud/__verify_no_such__",
                                     ["tvshow.nfo"], why=why)
            check("4) 两通道全断返回 None", txt is None)
            check("4) why 记满 2 条(逐条原因可进 502 文案)", len(why) == 2,
                  f"why={why}")
            check("4) why 里点名了 WebDAV 与 CD2 下载通道",
                  any("WebDAV" in w for w in why)
                  and any("CD2 下载通道" in w for w in why), f"why={why}")
            check("4) why 不再提 local_root/本地挂载(已下线)",
                  not any(("local_root" in w) or ("本地挂载" in w) for w in why),
                  f"why={why}")
        finally:
            nfo_mod.cd2.read_file_text = orig_read

        # ---------- 6) 闸门文案: 带上逐条原因, 不再引导配 local_root ----------
        if nfo_paths:
            folder_path, name = nfo_paths[0].rsplit("/", 1)
            from fastapi import HTTPException
            nfo_mod.cd2.read_file_text = lambda *a, **k: (None, "通道被测试关掉")
            try:
                # WebDAV 也一并掐掉, 逼出完整 502
                broken = copy.deepcopy(cfg)
                broken.setdefault("webdav", {})["base"] = ""
                try:
                    nfo_mod._rebuild_nfo(broken, "tv", 1, folder_path, name, "x")
                    check("5) 读不到时闸门抛 502", False, "没有抛异常")
                except HTTPException as e:
                    msg = str(e.detail)
                    check("5) 读不到时闸门抛 502", e.status_code == 502, msg[:120])
                    check("5) 文案带逐条通道原因(1) 与 2))",
                          "1)" in msg and "2)" in msg and "3)" not in msg, msg[:300])
                    check("5) 文案给出可执行的处理办法(CD2 网页 / account_root)",
                          "CD2" in msg and "account_root" in msg, msg[:300])
                    check("5) 文案不再引导配 local_root",
                          "local_root" not in msg, msg[:300])
            finally:
                nfo_mod.cd2.read_file_text = orig_read
        else:
            print("  -  5) 闸门文案: 没有可用的 /Cloud 剧集目录, 跳过")

        # ---------- 7) 正常配置仍走 WebDAV 快通道 ----------
        if created:
            t0 = time.time()
            txt = _existing_nfo_text(cfg, probe_folder, [probe_name])
            elapsed = time.time() - t0
            check("7) 正常配置读回探针", txt == marker, f"{elapsed:.2f}s")
    finally:
        if created:
            try:
                cd2.delete_file(cfg, probe_path)
                print(f"  探针已删除 {probe_path}")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ 探针删除失败(请手动清 {probe_path}): {str(e)[:120]}")

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
