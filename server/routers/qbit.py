"""qBittorrent 下载器路由: 配置 / 连接测试 / 推送 / 任务与进度(含剧集集数聚合)

设计(2026-09-19 用户需求):
  - 管理页可配 qbit 链接/账号, 一键连接测试。
  - 详情页/磁力列表可"推送 Qbit": 推送时打上 tag `ma:{kind}:{tmdb_id}`,
    让下载页能按作品聚合"下到第几集"。
  - 下载子页签: 全局统计 + 任务进度条/速度/状态 + 按作品分组(已下载/下载中/做种 + 涉及集号)。

⚠️ 客户端是同步的(httpx), 所有 qbit 调用都经 run_in_threadpool, 不在事件循环里阻塞。
未配置 qbit 时接口优雅返回 {ok:false, configured:false, msg: ...}, 前端据此提示去配置。
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from server.auth import require_auth
from server.config import CONFIG_PATH, get_config, reload_config
from db.database import SessionLocal
from db import repositories as repo

router = APIRouter(prefix="/api", tags=["qbit"], dependencies=[Depends(require_auth)])

# 标签格式: ma:{kind}:{tmdb_id} —— 推送时打, 下载页据此按作品聚合
_TAG_RE = re.compile(r"ma:(movie|tv):(\d+)")


def _configured(cfg):
    q = cfg.get("qbit", {}) or {}
    return bool((q.get("url") or "").strip() and (q.get("username") or "").strip())


class QbitPushBody(BaseModel):
    magnet: str
    kind: str = "movie"      # movie | tv
    tmdb_id: int = 0         # 打 tag 用; 0 = 不打
    title: str = ""
    toFolder: str = ""       # 保存路径(留空 = qbit 默认 / config.qbit.save_path)


class QbitConfigBody(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""
    save_path: str = ""
    category: str = ""


# ---------------------------------------------------------------------------
# 配置读写(管理页「下载」子页签的设置表单)
# ---------------------------------------------------------------------------
@router.get("/qbit/config")
async def qbit_config_get(cfg: dict = Depends(get_config)):
    """返回 qbit 配置(密码打码, 前端保存时空密码 = 不改动)。"""
    q = cfg.get("qbit", {}) or {}
    return {
        "configured": _configured(cfg),
        "url": q.get("url") or "",
        "username": q.get("username") or "",
        "password": (q.get("password") or "")[:3] + "•••" if q.get("password") else "",
        "password_set": bool((q.get("password") or "").strip()),
        "save_path": q.get("save_path") or "",
        "category": q.get("category") or "",
    }


@router.put("/qbit/config")
async def qbit_config_put(body: QbitConfigBody, cfg: dict = Depends(get_config)):
    """保存 qbit 配置 → 写回 config.json(热加载, 无需重启)。

    password 留空 = 保留原密码(前端打码后回传空, 不能把密码清空)。
    校验: url/username 非空; url 必须是合法 http(s) 地址。
    """
    url = (body.url or "").strip()
    user = (body.username or "").strip()
    if not url or not user:
        raise HTTPException(400, "qbit 地址与账号不能为空")
    if not re.match(r"^https?://[\w.-]+(:\d+)?(/.*)?$", url):
        raise HTTPException(400, f"qbit 地址不合法: {url}(应为 http://host:port)")

    def _write():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        q = dict(data.get("qbit") or {})
        q["url"] = url
        q["username"] = user
        # 密码: 留空/纯掩码 = 不改; 否则更新
        if (body.password or "").strip() and "•" not in body.password:
            q["password"] = body.password
        q["save_path"] = (body.save_path or "").strip()
        q["category"] = (body.category or "").strip()
        q["_comment"] = "qBittorrent WebAPI 下载器。url 填 WebUI 地址(如 http://host:8080), username/password 是 WebUI 账号。save_path/category 可选, 推送任务的默认保存路径与分类。"
        data["qbit"] = q
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        reload_config()
    try:
        await run_in_threadpool(_write)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"写入 config.json 失败: {e}")
    return {"ok": True, "msg": "qbit 配置已保存(热加载生效)"}


# ---------------------------------------------------------------------------
# 连接测试 + 全局统计
# ---------------------------------------------------------------------------
@router.get("/qbit/status")
async def qbit_status(cfg: dict = Depends(get_config)):
    """连接测试: 未配置返回 configured:false; 已配置则登录+拉版本/全局统计。"""
    if not _configured(cfg):
        return {"ok": False, "configured": False, "msg": "尚未配置 qBittorrent(到管理页「下载」填地址与账号)"}
    try:
        from clients.qbit import client as qbit
        st = await run_in_threadpool(qbit.status, cfg)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "configured": True, "msg": str(e)[:200]}
    return st


# ---------------------------------------------------------------------------
# 推送磁力(打 ma: tag 便于按作品聚合)
# ---------------------------------------------------------------------------
@router.post("/qbit/push")
async def qbit_push(body: QbitPushBody, cfg: dict = Depends(get_config)):
    """推一个磁力到 qBittorrent, 打 tag `ma:{kind}:{tmdb_id}`(tmdb_id>0 时)。"""
    if not _configured(cfg):
        raise HTTPException(503, "尚未配置 qBittorrent(到管理页「下载」填地址与账号)")
    if not (body.magnet or "").strip():
        raise HTTPException(400, "没有磁力链")
    kind = "tv" if body.kind == "tv" else "movie"
    tag = f"ma:{kind}:{body.tmdb_id}" if body.tmdb_id > 0 else ""

    def _do():
        from clients.qbit import client as qbit
        return qbit.add_torrent(cfg, body.magnet, tags=tag, save_path=body.toFolder)

    try:
        res = await run_in_threadpool(_do)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"推送到 qBittorrent 失败: {e}")
    # 写推送标记(落库, 重启不丢; info_hash 解析失败则跳过标记)
    try:
        from scripts import push as push_mod
        s = SessionLocal()
        try:
            repo.mark_pushed(s, push_mod.parse_info_hash(body.magnet), body.magnet, body.title, target="qbit")
            s.commit()
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        print(f"[qbit] 写入推送标记失败: {e}", file=sys.stderr)
    return {"ok": True, "tag": tag, "resp": res,
            "title": body.title}


# ---------------------------------------------------------------------------
# 任务列表 + 按作品聚合(集数)
# ---------------------------------------------------------------------------
@router.get("/qbit/torrents")
async def qbit_torrents(cfg: dict = Depends(get_config),
                        limit: int = 200, kind: str = ""):
    """任务进度列表 + 按 ma: tag 聚合"作品下载到第几集"。

    返回:
      torrents : 任务明细(原始字段 + _progress/_status 派生), 最多 limit 条
      shows    : 按作品聚合(只统计打了 ma: tag 的任务):
                 {kind, tmdb_id, title, torrents, downloading, seeding,
                  episodes:[{season,episode,state,progress,name}],
                  done_eps, down_eps, max_episode}
      transfer : 全局统计(下载/上传速度等)
    """
    if not _configured(cfg):
        return {"ok": False, "configured": False, "torrents": [], "shows": [],
                "msg": "尚未配置 qBittorrent"}
    limit = max(1, min(int(limit or 200), 1000))

    from clients.qbit import client as qbit
    try:
        torrents = await run_in_threadpool(qbit.list_torrents, cfg)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"读取 qBittorrent 任务失败: {e}")

    # 全局传输统计(失败不影响主流程)
    transfer = {}
    try:
        st = await run_in_threadpool(qbit.status, cfg)
        transfer = st.get("transfer") or {}
    except Exception:  # noqa: BLE001
        pass

    # 解析每个任务的 tag 与集号, 按 (kind, tmdb_id) 聚合
    from lib.naming import parse_episode
    groups = {}   # (kind, tmdb_id) -> {"episodes": [...], "downloading":0, "seeding":0, "count":0}
    for t in torrents:
        tags = t.get("tags") or ""
        if isinstance(tags, list):
            tags = ",".join(tags)
        m = _TAG_RE.search(tags)
        if not m:
            continue
        gkind, gtid = m.group(1), int(m.group(2))
        key = (gkind, gtid)
        g = groups.setdefault(key, {"episodes": [], "downloading": 0,
                                    "seeding": 0, "count": 0, "done_eps": set(), "down_eps": set()})
        g["count"] += 1
        # norm_status 的粗分类: downloading/queued(下载中) vs seeding(做种)
        from clients.qbit.client import norm_status
        _zh, cat2 = norm_status(t.get("state"))
        if cat2 in ("downloading", "queued"):
            g["downloading"] += 1
        elif cat2 == "seeding":
            g["seeding"] += 1
        # 解析集号(从任务名)
        season, episode = parse_episode(t.get("name") or "")
        if episode is not None:
            if cat2 == "seeding":
                g["done_eps"].add(episode)
            else:
                g["down_eps"].add(episode)
        g["episodes"].append({
            "season": season, "episode": episode,
            "state": t.get("state"), "status": t.get("_status"),
            "progress": t.get("_progress"), "name": (t.get("name") or "")[:120],
            "hash": t.get("hash"), "size": t.get("size"),
        })

    # 作品标题: 本地 TMDB 缓存查(查不到留空, 前端显 tmdb_id)
    def _titles():
        s = SessionLocal()
        try:
            out = {}
            for (gkind, gtid) in groups:
                row = repo.get_tmdb_media_by_id(s, gkind, gtid)
                out[(gkind, gtid)] = row.title if row else ""
            return out
        finally:
            s.close()

    titles = await run_in_threadpool(_titles) if groups else {}

    shows = []
    for (gkind, gtid), g in groups.items():
        all_eps = g["done_eps"] | g["down_eps"]
        max_ep = max(all_eps) if all_eps else 0
        shows.append({
            "kind": gkind, "tmdb_id": gtid,
            "title": titles.get((gkind, gtid), ""),
            "torrents": g["count"],
            "downloading": g["downloading"],
            "seeding": g["seeding"],
            "done_eps": sorted(g["done_eps"]),
            "down_eps": sorted(g["down_eps"]),
            "max_episode": max_ep,
            "episodes": g["episodes"],
        })
    # 排序: 下载中的在前, 其次按最新集号降序
    shows.sort(key=lambda x: (-(x["downloading"] or 0), -x["max_episode"], x["title"]))

    # 前端只展示 limit 条明细(shows 不受 limit 限制)
    return {
        "ok": True, "configured": True,
        "torrents": torrents[:limit],
        "shows": shows,
        "transfer": transfer,
    }
