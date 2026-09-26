"""标题与重命名路由
================================================================
两个端点, 都围绕「英文标题汉化」这条链路:

  PUT  /api/media/title/{kind}/{tmdb_id}     手动覆盖 / 清除中文标题(写 tmdb_media.custom_title)
  POST /api/media/rename/{kind}/{tmdb_id}    按**当前标题**重命名已入库的目录 + 文件 + NFO

标题优先级(manual > TMDB > 豆瓣 > TMDB 台/港)由 lib/titles.py 统一实现;
这里只负责落库(手动覆盖)与"改完之后把库里已经存在的东西改名"。

重命名的安全边界(用户铁律):
  - 只 rename, 不删任何东西 —— /Cloud 禁删;
  - 目标目录名已存在 → 409, 绝不覆盖;
  - 视频/字幕**就地改名**, 不移动位置、不重排季目录(只改名, 结构由整理负责);
  - 目标文件名已存在 → 跳过该文件(记进 skipped), 保证不覆盖同名文件;
  - 目录必须在 cloud_root 之下才动手。
"""
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from server.auth import require_auth
from server.config import get_config
from db.database import SessionLocal
from db import repositories as repo
from clients.clouddrive import client as cd2
from lib import naming
from scripts import organize

from server.routers.nfo import _build_meta, _dir_exists, _locate, _rebuild_nfo

router = APIRouter(prefix="/api", tags=["media"], dependencies=[Depends(require_auth)])


class TitleBody(BaseModel):
    title: str = ""


# ---------------------------------------------------------------------------
# 标题
# ---------------------------------------------------------------------------
@router.put("/media/title/{kind}/{tmdb_id}")
async def media_title(kind: str, tmdb_id: int, body: TitleBody,
                      cfg: dict = Depends(get_config)):
    """手动覆盖中文标题; 传空串 = 清除覆盖(重新走 TMDB > 豆瓣 > 台/港 的自动链路)。"""
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")

    new_title = (body.title or "").strip()
    if len(new_title) > 512:
        raise HTTPException(400, "标题过长(最多 512 字)")

    s = SessionLocal()
    try:
        obj = repo.get_tmdb_media_by_id(s, kind, tmdb_id)
        if obj is None:
            raise HTTPException(404, "本地没有这个作品的缓存(先搜索/同步一次)")
        repo.set_custom_title(s, kind, tmdb_id, new_title)
        if not new_title:
            # 清除覆盖 → 放开 title_checked, 并把 title 拨回原名(TMDB 英文/原语),
            # 否则下面的自动链路(apply_to_meta)会把"刚清掉的旧标题"当成库里已有中文直接沿用
            obj.title_checked = False
            obj.title = (obj.original_title or obj.english_title or "").strip() or obj.title
        s.commit()
    finally:
        s.close()

    # 让 title 与最终口径一致(设了 = 覆盖值; 清了 = 自动链路重算的结果)
    title = new_title
    meta = _build_meta(cfg, kind, tmdb_id)
    if meta and (meta.get("title") or "").strip():
        title = meta["title"].strip()
        s = SessionLocal()
        try:
            obj = repo.get_tmdb_media_by_id(s, kind, tmdb_id)
            if obj is not None:
                obj.title = title
                s.commit()
        finally:
            s.close()

    return {"ok": True, "title": title, "custom_title": new_title}


# ---------------------------------------------------------------------------
# 文件遍历 / 改名
# ---------------------------------------------------------------------------
def _list_dir(cfg, path):
    """列目录(失败抛可读 404, 而不是把 CD2 的 NOT_FOUND 炸成 500)。"""
    try:
        return cd2.get_subfiles(cfg, path) or []
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"CD2 找不到目录: {path} — {e}") from e


def _walk_media(cfg, path, depth=0, vids=None, subs=None):
    """递归收集目录树里的视频与外挂字幕(最多 4 层)。"""
    if vids is None:
        vids, subs = [], []
    if depth > 4:
        return vids, subs
    for it in _list_dir(cfg, path):
        if cd2.is_dir(it):
            child = it.get("fullPathName") or f"{path.rstrip('/')}/{it.get('name')}"
            _walk_media(cfg, child, depth + 1, vids, subs)
            continue
        name = it.get("name") or ""
        ext = (os.path.splitext(name)[1] or "").lower()
        if ext in naming.VIDEO_EXT:
            vids.append({"dir": path, "name": name, "size": it.get("size") or 0})
        elif ext in naming.SUBTITLE_EXT:
            subs.append({"dir": path, "name": name})
    return vids, subs


def _target_free(cfg, parent, new_name, taken):
    """目标名是否可用(同目录里已存在 → False; 本批已占用 → False)。"""
    if new_name.lower() in taken:
        return False
    try:
        return not cd2.find_file_by_path(cfg, parent, new_name)
    except Exception:  # noqa: BLE001
        return False


def _sub_tail(old_stem, sub_name, new_stem):
    """字幕跟随视频改名: 保留 '.chs' / '.zh' 这类尾巴。"""
    stem, sext = os.path.splitext(sub_name)
    low = stem.lower()
    tail = ""
    if low == old_stem.lower():
        tail = ""
    elif low.startswith(old_stem.lower() + "."):
        tail = stem[len(old_stem):]
    else:
        return ""
    return new_stem + tail + sext


_TV_SHAPE = re.compile(r"^(?P<head>.+?)\s*-\s*(?P<se>S\d{2}(?:E\d{2})?)\s*-\s*(?P<tail>.+)$",
                       re.IGNORECASE)


def _rename_files(cfg, kind, meta, vids, subs, apply=True, old_folder="", new_folder=""):
    """就地把视频/字幕的**标题段**换成新标题(**不移动**, 质量/编码/季集一律原样保留)。

    为什么不用模板重排文件名: 文件名里的质量标记(480p/720p)是整理时用 MediaInfo
    实探出来的, 按文件名反推会把 480p 改成 720p(实测 僵尸道长 S01E01),
    所以只替换标题那一段, 其余字符保持不变。
    apply=False 只算不改(dry_run)。返回 (renamed, skipped)。"""
    renamed, skipped = [], []
    title = (meta.get("title") or "").strip() or "未命名"
    old_folder = (old_folder or "").strip()
    new_folder = (new_folder or "").strip()
    old_title = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", old_folder).strip()
    by_dir = {}
    for sub in subs:
        by_dir.setdefault(sub["dir"], []).append(sub)

    def _do(dirpath, old_name, new_name):
        if new_name == old_name:
            return False
        if not _target_free(cfg, dirpath, new_name, {n.lower() for _a, n in renamed}):
            skipped.append(f"{old_name} → {new_name}(目标名已存在)")
            return False
        if apply:
            try:
                cd2.rename_file(cfg, f"{dirpath.rstrip('/')}/{old_name}", new_name)
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{old_name}: {str(e)[:80]}")
                return False
        renamed.append((old_name, new_name))
        return True

    if kind == "movie":
        # 与整理同口径: 只动「唯一一个视频」(多文件不动, 避免误伤)
        if len(vids) != 1:
            if len(vids) > 1:
                skipped.append(f"有 {len(vids)} 个视频文件, 只改唯一视频的规则不适用")
            return renamed, skipped
        v = vids[0]
        stem, ext = os.path.splitext(v["name"])
        new_stem = None
        if old_folder and stem.startswith(old_folder):
            new_stem = new_folder + stem[len(old_folder):]      # '盗梦空间 (2010) 1080p' → '新标题 (2010) 1080p'
        elif old_title and stem.startswith(old_title):
            new_stem = title + stem[len(old_title):]
        if not new_stem:
            skipped.append(f"{v['name']}(文件名不以原标题开头, 保持原名)")
            return renamed, skipped
        if _do(v["dir"], v["name"], new_stem + ext):
            for sub in by_dir.get(v["dir"], []):
                new_sub = _sub_tail(stem, sub["name"], new_stem)
                if new_sub:
                    _do(sub["dir"], sub["name"], new_sub)
        return renamed, skipped

    # 剧集: 只把 '标题 - SxxEyy - 质量' 里的标题段换掉, 季集号与质量段原样保留
    for v in vids:
        stem, ext = os.path.splitext(v["name"])
        m = _TV_SHAPE.match(stem)
        if not m:
            skipped.append(f"{v['name']}(不是「标题 - SxxEyy - 质量」结构, 保持原名)")
            continue
        new_stem = f"{title} - {m.group('se')} - {m.group('tail')}"
        if new_stem == stem:
            continue
        if _do(v["dir"], v["name"], new_stem + ext):
            for sub in by_dir.get(v["dir"], []):
                new_sub = _sub_tail(stem, sub["name"], new_stem)
                if new_sub:
                    _do(sub["dir"], sub["name"], new_sub)
    return renamed, skipped


# ---------------------------------------------------------------------------
# 重命名
# ---------------------------------------------------------------------------
@router.post("/media/rename/{kind}/{tmdb_id}")
async def media_rename(kind: str, tmdb_id: int, dry_run: bool = False,
                        cfg: dict = Depends(get_config)):
    """按当前标题(custom_title > 中文自动标题)重命名: 目录 → 文件 → NFO。

    dry_run=1 → 只算不改, 返回将要发生的改名(真改之前先看一眼)。"""
    if kind not in ("movie", "tv"):
        raise HTTPException(400, "kind 仅支持 movie / tv")

    def _q():
        meta = _build_meta(cfg, kind, tmdb_id)
        if not meta:
            raise HTTPException(502, "无法获取元数据(TMDB 不可达且本地无缓存)")
        title = (meta.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "标题为空,无法重命名")

        folder_path, nfo_name, _fname, cloud = _locate(cfg, kind, tmdb_id)
        if not folder_path or not _dir_exists(cfg, folder_path):
            raise HTTPException(404, "找不到媒体所在目录(可能尚未整理入库)")

        folder_path = folder_path.rstrip("/")
        cloud = (cloud or organize.cloud_root(cfg)).rstrip("/")
        if cloud and folder_path != cloud and not folder_path.startswith(cloud + "/"):
            raise HTTPException(400, f"目录不在云根之下,拒绝重命名: {folder_path}")

        parent, old_name = os.path.dirname(folder_path), os.path.basename(folder_path)
        new_name = naming.folder_name_from_meta(
            {"title": title, "year": meta.get("year") or ""},
            organize.folder_template(cfg))

        dir_changed, dir_conflict = False, ""
        new_dir = folder_path
        if new_name and new_name != old_name:
            for it in _list_dir(cfg, parent):
                if cd2.is_dir(it) and (it.get("name") or "") == new_name:
                    if dry_run:
                        dir_conflict = f"{parent}/{new_name}"
                        break
                    raise HTTPException(409, f"目标目录已存在,拒绝覆盖: {parent}/{new_name}")
            if not dir_conflict:
                new_dir = f"{parent}/{new_name}"
                dir_changed = True
                if not dry_run:
                    try:
                        cd2.rename_file(cfg, folder_path, new_name)
                    except Exception as e:  # noqa: BLE001
                        raise HTTPException(502, f"目录改名失败: {e}") from e

        walk_root = folder_path if dry_run else new_dir
        vids, subs = _walk_media(cfg, walk_root)
        renamed, skipped = _rename_files(cfg, kind, meta, vids, subs,
                                         apply=not dry_run,
                                         old_folder=old_name, new_folder=new_name)

        # 电影 NFO 文件名跟随视频基名(剧集固定 tvshow.nfo)
        nfo_renamed = ""
        if kind == "movie":
            expected = None
            if renamed:
                for _old, new in renamed:
                    if os.path.splitext(new)[1].lower() in naming.VIDEO_EXT:
                        expected = os.path.splitext(new)[0] + ".nfo"
                        break
            if expected:
                cur = None
                try:
                    for it in _list_dir(cfg, walk_root):
                        if not cd2.is_dir(it) and (it.get("name") or "").lower().endswith(".nfo"):
                            cur = it.get("name")
                            if cur == expected:
                                break
                except Exception:  # noqa: BLE001
                    cur = None
                if cur and cur != expected:
                    if dry_run:
                        nfo_renamed = f"{cur} → {expected}"
                    else:
                        try:
                            cd2.rename_file(cfg, f"{walk_root}/{cur}", expected)
                            nfo_renamed = expected
                        except Exception:  # noqa: BLE001
                            nfo_renamed = ""
                nfo_name = expected or (nfo_name or "")
        elif not nfo_name:
            nfo_name = "tvshow.nfo"

        remapped, nfo = 0, None
        if not dry_run:
            # 本地 jellyfin 镜像路径跟着走(下次 _locate 直接命中)
            s = SessionLocal()
            try:
                remapped = repo.remap_jellyfin_paths(s, folder_path, new_dir,
                                                      file_renames=renamed)
                s.commit()
            finally:
                s.close()

            try:
                nfo = _rebuild_nfo(cfg, kind, tmdb_id, new_dir, nfo_name, new_name)
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001
                nfo = None

        return {"ok": True, "dry_run": dry_run, "dir": new_dir,
                "dir_renamed": dir_changed, "dir_conflict": dir_conflict,
                "from": folder_path, "title": title,
                "files": [{"from": a, "to": b} for a, b in renamed],
                "skipped": skipped, "nfo_renamed": nfo_renamed,
                "jellyfin_paths_updated": remapped,
                "nfo": (nfo or {}).get("path", ""), "warning": (nfo or {}).get("warning", "")}

    return await run_in_threadpool(_q)
