"""配置读写路由(管理页「通用」子页签 + 首次引导)

配置真相源是 SQLite 的 app_config(见 db/models.AppConfig), 运行期只读数据库:
配置文件仅在首启被一次性导入(之后失效), 想改配置只能走这里。本模块提供:
  GET  /api/config          整份配置(敏感值打码)+ DB 路径 + 引导完成标记
  PUT  /api/config          整份保存(掩码回填原值 → 校验 → 写 DB → 热加载)
  GET  /api/config/export   导出完整 JSON(含令牌, 用于备份/迁移)
  GET  /api/config/setup    首次引导的检查项(哪些必填还缺、对应能启用什么)
  POST /api/config/setup/done  标记引导完成

打码规则: token/password/api_key/authorization 等敏感字段在 GET 时变成 MASK,
前端回传仍是 MASK 的值表示"没改", 服务端还原成原值 —— 明文令牌不进浏览器。
"""
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel

from server.auth import require_auth
from server.config import get_config, reload_config, save_config

router = APIRouter(prefix="/api", tags=["config"], dependencies=[Depends(require_auth)])

MASK = "••••••"
_SECRET_RE = re.compile(r"(token|password|passwd|secret|api_?key|authorization|credential|jwt)", re.I)

# 首次引导的必填项(path, 标题, 配好才能用什么)。顺序 = 引导步骤顺序。
_SETUP_STEPS = [
    ("web.auth.password", "管理员密码", "登录 Web 控制台(默认 change_me 不安全)"),
    ("jellyfin.url", "Jellyfin 地址", "媒体库同步、缺失检测、入库刷新"),
    ("jellyfin.token", "Jellyfin API Key", "扫描媒体库与写回观看进度"),
    ("tmdb.api_key", "TMDB API Key", "元数据刮削、中文名反查、缺失检测"),
    ("clouddrive2.hosts", "CloudDrive2 地址", "读离线目录、移动归位"),
    ("clouddrive2.token", "CD2 令牌", "推送离线下载、查任务状态"),
    ("organize.cloud_root", "媒体库根目录", "整理归位的目标路径(如 /Cloud)"),
]
_PLACEHOLDERS = ("", "change_me", "YOUR_TMDB_API_KEY", "YOUR_CD2_API_TOKEN_OR_JWT")


class ConfigBody(BaseModel):
    config: dict


def _mask(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_RE.search(str(k)) and isinstance(v, str) and v.strip():
                out[k] = MASK
            else:
                out[k] = _mask(v)
        return out
    if isinstance(obj, list):
        return [_mask(v) for v in obj]
    return obj


def _unmask(new: Any, old: Any) -> Any:
    if isinstance(new, dict):
        old = old if isinstance(old, dict) else {}
        out = {}
        for k, v in new.items():
            if _SECRET_RE.search(str(k)) and v == MASK:
                prev = old.get(k, "")
                out[k] = prev if isinstance(prev, str) else ""
            else:
                out[k] = _unmask(v, old.get(k))
        return out
    if isinstance(new, list):
        old = old if isinstance(old, list) else []
        return [_unmask(v, old[i] if i < len(old) else None) for i, v in enumerate(new)]
    return new


def _get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _configured(cfg: dict, path: str) -> bool:
    v = _get_path(cfg, path)
    if isinstance(v, str):
        return v.strip() not in _PLACEHOLDERS
    if isinstance(v, list):
        return len(v) > 0
    return bool(v)


def _validate(data: dict) -> None:
    port = _get_path(data, "web.port")
    if port is not None and str(port).strip() != "":
        try:
            p = int(port)
        except (TypeError, ValueError):
            raise HTTPException(400, "web.port 必须是整数")
        if not 1 <= p <= 65535:
            raise HTTPException(400, "web.port 必须在 1–65535 之间")
        _set_path(data, "web.port", p)
    for key in ("library_root", "organize.cloud_root", "organize.staging_dir"):
        v = _get_path(data, key)
        if isinstance(v, str) and v.strip() and not v.strip().startswith("/"):
            raise HTTPException(400, f"{key} 必须是以 / 开头的绝对路径: {v}")
    if not isinstance(_get_path(data, "categories"), (dict, type(None))):
        raise HTTPException(400, "categories 必须是对象")


@router.get("/config")
async def api_config_get(cfg: dict = Depends(get_config)):
    from lib.config import db_path, is_setup_done  # noqa: PLC0415
    return {
        "ok": True,
        "db": db_path(),
        "setup_done": is_setup_done(),
        "config": _mask(cfg),
    }


@router.put("/config")
async def api_config_put(body: ConfigBody):
    incoming = body.config
    if not isinstance(incoming, dict) or not incoming:
        raise HTTPException(400, "配置内容必须是非空 JSON 对象")
    current = get_config()
    data = _unmask(incoming, current)
    _validate(data)
    try:
        await run_in_threadpool(save_config, data)   # 写库不占事件循环
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"写入配置失败: {e}")
    return {"ok": True, "msg": "配置已保存并热加载, 立即生效"}


@router.get("/config/export")
async def api_config_export(cfg: dict = Depends(get_config)):
    """导出完整配置(不打码)—— 备份与跨机器迁移用, 注意文件里含令牌。"""
    body = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="mediaauto-config.json"'},
    )


@router.get("/config/setup")
async def api_config_setup(cfg: dict = Depends(get_config)):
    from lib.config import is_setup_done  # noqa: PLC0415
    return {
        "done": is_setup_done(),
        "checks": {path: _configured(cfg, path) for path, _, _ in _SETUP_STEPS},
        "steps": [{"path": p, "title": t, "feature": f} for p, t, f in _SETUP_STEPS],
    }


@router.post("/config/setup/done")
async def api_config_setup_done():
    def _mark():
        from lib.config import set_setup_done  # noqa: PLC0415
        set_setup_done(True)
    try:
        await run_in_threadpool(_mark)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"标记引导完成失败: {e}")
    reload_config()
    return {"ok": True, "msg": "引导已完成"}
