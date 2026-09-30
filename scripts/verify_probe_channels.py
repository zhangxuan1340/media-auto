#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【真 CD2】<fileinfo> 探测的两条通道回归(local_root 下线后)

2026-09-30: `clouddrive2.local_root` 本地挂载通道**已全面下线**, lib.mediainfo.probe()
只剩两条通道:
  1) CD2 WebDAV    -> ffprobe over HTTP(Range), mediainfo CLI 兜底(受 account_root 限制)
  2) CD2 下载链接   -> ffprobe over HTTP(只要 gRPC token, 不受 account_root 限制)
本脚本造一个 1 秒的测试 mp4 放进 /Temp, 分别在「账号覆盖到 /Temp」与「越界」两种配置下
断言 source == webdav / download, 并守两条下线红线:
  - mediainfo 不再有 local_path/local_roots;
  - 两条通道都断时, 失败原因里点名 WebDAV 与下载链接, 且不提 local_root。

用法: ./venv/bin/python scripts/verify_probe_channels.py
退出码: 0 = 全部通过(或工具缺失时跳过); 1 = 有失败项
"""
import copy
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []
STAGE_DIR = "/Temp/.media_auto_stage"
TEST_NAME = "verify_probe_chan.mp4"


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def make_test_mp4(path):
    """ffmpeg 造一个 1 秒 320x240 + 440Hz 音的 mp4(几百 KB, 走 CD2 上传无压力)。"""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=15",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(path):
        print(f"  ⚠ 造测试视频失败: {(proc.stderr or '')[:200]}")
        return False
    return True


def main():
    from lib import config as lc
    from lib import mediainfo
    from clients.clouddrive import client as cd2

    # ---------- 0) local_root 通道已删干净 ----------
    check("0) mediainfo 无 local_path/local_roots",
          not hasattr(mediainfo, "local_path") and not hasattr(mediainfo, "local_roots"))

    # ---------- 工具与连通性 ----------
    if not shutil.which("ffprobe"):
        print("  -  本机没有 ffprobe, 跳过(装了 ffmpeg 才能跑)")
        print("\n-- 检查结果 --\n  全部通过(跳过)")
        return 0

    cfg = lc.load_config()
    remote = f"{STAGE_DIR}/{TEST_NAME}"
    timeout = mediainfo.probe_timeout(cfg)

    tmpdir = tempfile.mkdtemp(prefix="probe_chan_")
    local = os.path.join(tmpdir, TEST_NAME)
    uploaded = False
    try:
        if not make_test_mp4(local):
            check("造测试视频", False, "ffmpeg 失败")
            return 1
        try:
            cd2.ensure_folder(cfg, STAGE_DIR.rsplit("/", 1)[0], STAGE_DIR.rsplit("/", 1)[1])
            cd2.write_file(cfg, remote, Path(local).read_bytes())
            uploaded = True
            print(f"  测试视频已上传 {remote}")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ 上传失败(CD2 不通?), 跳过通道用例: {str(e)[:160]}")

        if uploaded:
            # ---------- 1) 账号覆盖 /Temp → WebDAV 主通道 ----------
            info = mediainfo.probe(remote, cfg, timeout=timeout)
            check("1) WebDAV 通道探到(含视频流)",
                  bool(info) and info.get("source") == "webdav"
                  and (info.get("video") or {}).get("width") == 320,
                  f"source={(info or {}).get('source')}")
            if info:
                print(f"       source={info.get('source')} "
                      f"{info['video'].get('resolution_label')} "
                      f"{info['video'].get('codec')} {info.get('duration_secs')}s")

            # ---------- 2) 账号越界 → CD2 下载链接通道 ----------
            out_of_range = copy.deepcopy(cfg)
            out_of_range.setdefault("webdav", {})["account_root"] = "/Cloud"
            info2 = mediainfo.probe(remote, out_of_range, timeout=timeout)
            check("2) WebDAV 越界时改走 CD2 下载链接(source=download)",
                  bool(info2) and info2.get("source") == "download"
                  and (info2.get("video") or {}).get("width") == 320,
                  f"source={(info2 or {}).get('source')}, "
                  f"reasons 不可见(需 log)")

            # ---------- 3) WebDAV 关掉 → 仍能走下载链接 ----------
            wd_off = copy.deepcopy(cfg)
            wd_off.setdefault("webdav", {})["enabled"] = False
            info3 = mediainfo.probe(remote, wd_off, timeout=timeout)
            check("3) webdav.enabled=false 时仍走下载链接",
                  bool(info3) and info3.get("source") == "download",
                  f"source={(info3 or {}).get('source')}")

        # ---------- 4) 两条通道全断 → None + 逐条原因(不提 local_root) ----------
        both_off = copy.deepcopy(cfg)
        both_off.setdefault("webdav", {})["enabled"] = False
        notes = []
        orig = cd2.download_urls
        cd2.download_urls = lambda *a, **k: ([], "下载链接被测试关掉")
        try:
            info4 = mediainfo.probe("/Cloud/__verify_no_such__/x.mkv", both_off,
                                    timeout=15, log=notes.append)
        finally:
            cd2.download_urls = orig
        joined = " ".join(str(n) for n in notes)
        check("4) 两通道全断返回 None(写空标签而非假数据)", info4 is None)
        check("4) 失败原因点名 WebDAV 与下载链接",
              "WebDAV" in joined and "下载链接" in joined, joined[:300])
        check("4) 失败原因不提 local_root/本地挂载",
              "local_root" not in joined and "本地挂载" not in joined, joined[:300])
    finally:
        if uploaded:
            try:
                cd2.delete_file(cfg, remote)
                print(f"  测试视频已删除 {remote}")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠ 删除失败(请手动清 {remote}): {str(e)[:120]}")
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n-- 检查结果 --")
    if FAILED:
        print(f"  {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
