#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""【沙箱】验证用户铁律: "程序在 Temp 离线下载目录里面整理好, 再移动到对应的 Cloud 目录中去。"

为什么必须这样: `/Cloud` 只准往里移、**禁删**。任何整理(剧集按季归位、电影改名、清掉搬空的
旧季包目录)只要发生在搬运之后, 留在 /Cloud 里的空目录/半成品名就**永远清不掉**。

本脚本在 `/Temp/_verify_org` 下自建三条真实场景树, 真跑 apply_plan 用的同一条整理路径
`_organize_in_workspace`(剧集→按季归位+改名+清空壳; 电影→改 TMM 标准名; 散落文件→先收入
规范目录再整理), 逐个断言后自动清理。**只动 /Temp(可删), 全程不碰 /Cloud**。

用法:  venv/bin/python scripts/verify_organize.py [--keep]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.clouddrive import client as cd2      # noqa: E402
from lib import config as cfgmod                  # noqa: E402
from server.config import skill_root              # noqa: E402
import scripts.organize as og                      # noqa: E402

ROOT = "/Temp/_verify_org"

# ============================ 场景 A: 剧集多季合集 ============================
TV_DIR = ROOT + "/欢乐合唱团 (2009)"
TV_TREE = {
    # 旧季包·已搬空 → 应被清掉
    "欢乐合唱团.S01.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV": {},
    # 旧季包·裸集号(无 Sxx, 季号只在目录名) → 进 Season 2 并改名; 字幕跟随
    "欢乐合唱团.S02.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV": {
        "E01.mkv": "x", "E02.mkv": "x", "E01.chs.srt": "x"},
    # 旧季包·规范 SxxExx 名 → 进 Season 3 并改名; 无尾巴字幕也跟着视频改名
    "欢乐合唱团.S03.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV": {
        "欢乐合唱团.S03E01.1080p.DSNP.mkv": "x",
        "欢乐合唱团.S03E01.1080p.DSNP.srt": "x"},
    # 已就位(质量与 plan 一致) → 原样保留, 不被降级改名
    "Season 4": {"欢乐合唱团 - S04E01 - 1080p h264 EAC3.mkv": "x"},
    # 深空目录 → 应被清掉
    "empty_nested/a/b": {},
}
TV_ROOT_FILES = {"tvshow.nfo": "<tvshow></tvshow>"}
TV_PRESENT = [
    "tvshow.nfo",
    "Season 2/欢乐合唱团 - S02E01 - 1080p h264 EAC3.mkv",
    "Season 2/欢乐合唱团 - S02E02 - 1080p h264 EAC3.mkv",
    "Season 2/欢乐合唱团 - S02E01 - 1080p h264 EAC3.chs.srt",
    "Season 3/欢乐合唱团 - S03E01 - 1080p h264 EAC3.mkv",
    "Season 3/欢乐合唱团 - S03E01 - 1080p h264 EAC3.srt",
    "Season 4/欢乐合唱团 - S04E01 - 1080p h264 EAC3.mkv",
]
TV_MISSING = [
    "欢乐合唱团.S01.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV",
    "欢乐合唱团.S02.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV",
    "欢乐合唱团.S03.1080p.DSNP.WEB-DL.DDP.5.1.H.264-BlackTV",
    "empty_nested",
]

# ============================ 场景 B: 电影(整目录) ============================
MV_DIR = ROOT + "/后室 (2026)"
MV_FILES = {
    "BackRooms.2026.CAN.UHD.BluRay.2160p.HDR10.mkv": "x",
    "BackRooms.2026.CAN.UHD.BluRay.2160p.HDR10.chs.srt": "x",
    "不相关字幕.srt": "x",          # 与视频主干无关 → 不该被牵连改名
}
MV_TREE = {
    "空目录/": {},                  # 空目录 → 应被清掉(否则进 /Cloud 永远删不掉)
}

# ======================= 场景 C: 散落文件(没有自己的目录) =======================
LOOSE_PARENT = ROOT + "/_loose"
LOOSE_NAME = "Some.Movie.2020.1080p.WEB-DL.AAC.H264.mkv"
LOOSE_SUB = "Some.Movie.2020.1080p.WEB-DL.AAC.H264.srt"
LOOSE_DIR_NAME = "某电影 (2020)"


def _mkdirs(cfg, base, path):
    """逐级建目录(已存在则忽略)。"""
    cur = ""
    for p in path.strip("/").split("/"):
        parent, cur = (cur or "/"), ((cur + "/" + p) if cur else ("/" + p))
        if parent == "/" and cur == "/Temp":
            continue
        try:
            cd2.create_folder(cfg, parent, p, base_dir=base)
        except Exception:                 # noqa: BLE001
            pass


def _put(cfg, base, path, content="x"):
    parent, name = os.path.dirname(path), os.path.basename(path)
    cd2.write_file(cfg, parent.rstrip("/") + "/" + name, content, base_dir=base)


def _build_tree(cfg, base, root_dir, tree, root_files=None):
    _mkdirs(cfg, base, root_dir)
    for fn, content in (root_files or {}).items():
        _put(cfg, base, root_dir + "/" + fn, content)
    for sub, files in tree.items():
        d = root_dir + "/" + sub.rstrip("/")
        _mkdirs(cfg, base, d)
        for fn, content in files.items():
            _put(cfg, base, d + "/" + fn, content)


def _listing(cfg, base, path):
    """目录内容 {名字: is_dir}。"""
    out = {}
    for s in cd2.get_subfiles(cfg, path, base_dir=base) or []:
        out[s.get("name") or ""] = cd2.is_dir(s)
    return out


def _exists(cfg, base, root_dir, rel):
    """root_dir 下相对路径是否存在(逐级查)。"""
    cur, parts = root_dir, [p for p in rel.split("/") if p]
    for i, p in enumerate(parts):
        if p not in _listing(cfg, base, cur):
            return False
        if i == len(parts) - 1:
            return True
        cur = cur + "/" + p
    return False


def _cleanup(cfg, base):
    try:
        cd2.delete_files(cfg, [ROOT], base_dir=base, force=True)
    except Exception as e:  # noqa: BLE001
        print("  [cleanup] 失败:", str(e)[:140])


def _probe_info(res="1080p", codec="h264", audio="EAC3"):
    """构造一份"搬运前媒体探测结果", 让 quality_of_plan 取到确定的质量标记(走真实代码路径)。"""
    return {"video": {"resolution_label": res, "codec_label": codec},
            "audios": [{"codec_label": audio}]}


def _tv_plan(cfg, base):
    entry = {"name": os.path.basename(TV_DIR), "fullPathName": TV_DIR, "isDirectory": True}
    plan = og.analyse_entry(cfg, entry, base_dir=base, read_only=True)
    plan["meta"] = {"kind": "tv", "title": "欢乐合唱团"}
    # 质量固定成 '1080p h264 EAC3': 与树里"已就位"的 Season 4 名一致, 才能验证"不被降级改名"
    plan["_probe_info"] = _probe_info("1080p", "h264", "EAC3")
    return plan


def _movie_plan(cfg, base):
    entry = {"name": os.path.basename(MV_DIR), "fullPathName": MV_DIR, "isDirectory": True}
    plan = og.analyse_entry(cfg, entry, base_dir=base, read_only=True)
    plan["meta"] = {"kind": "movie", "title": "后室", "year": 2026}
    plan["_probe_info"] = _probe_info("2160p", "h265", "EAC3")
    return plan


def case_tv(cfg, base, fails):
    print("\n── 场景 A: 剧集多季合集(旧季包残留) ──")
    plan = _tv_plan(cfg, base)
    print("   media=%d subtitles=%d" % (len(plan["media"]), len(plan["subtitles"])))
    mv = og._organize_in_workspace(cfg, plan, TV_DIR, base_dir=base, log=lambda m: print("    " + m))
    print("   整理 %d 个文件" % len(mv))
    for rel in TV_PRESENT:
        if not _exists(cfg, base, TV_DIR, rel):
            fails.append("[A] 缺失: " + rel)
    for rel in TV_MISSING:
        if _exists(cfg, base, TV_DIR, rel):
            fails.append("[A] 残留未清: " + rel)
    stray = [n for n, d in _listing(cfg, base, TV_DIR).items()
             if d and not n.startswith("Season ")]
    for n in stray:
        fails.append("[A] 顶层残留目录: " + n)


def case_movie(cfg, base, fails):
    print("\n── 场景 B: 电影整目录(视频+字幕改为 TMM 标准名) ──")
    plan = _movie_plan(cfg, base)
    print("   media=%d subtitles=%d" % (len(plan["media"]), len(plan["subtitles"])))
    mv = og._organize_in_workspace(cfg, plan, MV_DIR, base_dir=base, log=lambda m: print("    " + m))
    print("   改名:", mv)
    for rel in ["后室 (2026) 2160p h265 EAC3.mkv",
                "后室 (2026) 2160p h265 EAC3.chs.srt"]:
        if not _exists(cfg, base, MV_DIR, rel):
            fails.append("[B] 缺失: " + rel)
    for rel in ["BackRooms.2026.CAN.UHD.BluRay.2160p.HDR10.mkv", "空目录"]:
        if _exists(cfg, base, MV_DIR, rel):
            fails.append("[B] 残留未清/未改名: " + rel)
    if not _exists(cfg, base, MV_DIR, "不相关字幕.srt"):
        fails.append("[B] 无关字幕被误改名/误删: 不相关字幕.srt")


def case_loose(cfg, base, fails):
    print("\n── 场景 C: 散落文件(先收入规范目录, 再在 /Temp 内整理) ──")
    _mkdirs(cfg, base, LOOSE_PARENT)
    _put(cfg, base, LOOSE_PARENT + "/" + LOOSE_NAME)
    _put(cfg, base, LOOSE_PARENT + "/" + LOOSE_SUB)
    # 诱饵: 离线根里已有一个同名条目目录 —— 绝不能被"顺手"整体搬走
    decoy = LOOSE_PARENT + "/" + LOOSE_DIR_NAME
    _mkdirs(cfg, base, decoy)
    _put(cfg, base, decoy + "/别人的文件.mkv", "x")

    plan = {
        "source": LOOSE_PARENT + "/" + LOOSE_NAME,
        "name": LOOSE_NAME, "is_dir": False,
        "media": [{"name": LOOSE_NAME, "path": LOOSE_PARENT + "/" + LOOSE_NAME,
                   "size": 1, "rel_dir": ""}],
        "subtitles": [LOOSE_PARENT + "/" + LOOSE_SUB],
        "ads": [], "junk": [], "nfos": [], "assets": [],
        "meta": {"kind": "movie", "title": "某电影", "year": 2020},
        "status": "ok",
        "_probe_info": _probe_info("1080p", "h264", "AAC"),
    }
    # 复刻 apply_plan 的散落文件分支: 中转区建规范目录 → 收入 → 工作区内整理
    stage = og._ensure_path(cfg, cd2.staging_dir(cfg), base_dir=base)
    work_dir = cd2.ensure_folder(cfg, stage, LOOSE_DIR_NAME, base_dir=base)
    if cd2.get_subfiles(cfg, work_dir, base_dir=base):
        work_name = "%s.__stage__%d" % (LOOSE_DIR_NAME, int(og.time.time()))
        work_dir = cd2.ensure_folder(cfg, stage, work_name, base_dir=base)
    cd2.move_file(cfg, [plan["source"], plan["subtitles"][0]], work_dir, base_dir=base)
    mv = og._organize_in_workspace(cfg, plan, work_dir, base_dir=base, log=lambda m: print("    " + m))
    print("   改名:", mv)
    for rel in ["某电影 (2020) 1080p h264 AAC.mkv", "某电影 (2020) 1080p h264 AAC.srt"]:
        if not _exists(cfg, base, work_dir, rel):
            fails.append("[C] 缺失: " + rel)
    if _exists(cfg, base, LOOSE_PARENT, LOOSE_NAME):
        fails.append("[C] 散落文件未收入工作目录: " + LOOSE_NAME)
    if not _exists(cfg, base, decoy, "别人的文件.mkv"):
        fails.append("[C] 误动离线根里同名条目目录(诱饵被搬走): " + LOOSE_DIR_NAME)
    try:                                   # 清掉中转区工作目录(它不在 ROOT 下)
        cd2.delete_files(cfg, [work_dir], base_dir=base, force=True)
    except Exception:                      # noqa: BLE001
        pass


def main():
    keep = "--keep" in sys.argv
    cfg = cfgmod.load_config()
    base = skill_root()

    print("=" * 70)
    print("沙箱: '先在 /Temp 离线目录整理好, 再移到 /Cloud'   (只动 /Temp, 不碰 /Cloud)")
    print("=" * 70)

    _cleanup(cfg, base)
    _build_tree(cfg, base, TV_DIR, TV_TREE, TV_ROOT_FILES)
    _build_tree(cfg, base, MV_DIR, MV_TREE, MV_FILES)
    print("三条场景树已建:", ROOT)

    fails = []
    case_tv(cfg, base, fails)
    case_movie(cfg, base, fails)
    case_loose(cfg, base, fails)

    if not keep:
        _cleanup(cfg, base)
        print("\n沙箱已清理")
    else:
        print("\n--keep: 保留沙箱", ROOT)

    print("-" * 70)
    if fails:
        print("❌ 失败 %d 项:" % len(fails))
        for f in fails:
            print("   - " + f)
        return 1
    print("✅ 全部通过: 剧集旧季包全清+按季归位 / 电影改标准名+无关字幕不受影响+空目录清掉 /")
    print("   散落文件先收入规范目录再整理 —— 均在 /Temp 工作区内完成, 进 /Cloud 的即为成品。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
