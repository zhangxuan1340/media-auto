"""MediaAuto Web —— 单端口入口

一个 FastAPI 进程同时提供:
  - 网页 UI(/ 与 /api/auth/* 公开)
  - 业务 API(/api/browse, /api/search, /api/push, /api/organize/*, /api/sync/*, /api/local/*, 均需登录)

直接复用项目根的 lib/、clients/、scripts/ 与 db/: classify / cd2 / state / jellyfin / sync。
(浏览/搜索/缺失/详情全走本地 TMDB 缓存。)
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server.auth import COOKIE_NAME, current_creds, expected_token, is_authed, make_token
from server.config import CONFIG, PROJECT_ROOT, get_config
from server.routers import browse as browse_router
from server.routers import cd2 as cd2_router
from server.routers import configapi as config_router
from server.routers import jobs as jobs_router
from server.routers import nfo as nfo_router
from server.routers import qbit as qbit_router
from server.routers import search as search_router
from server.routers import sync as sync_router
from server.routers import track as track_router
from server import imgproxy as imgproxy_router


def _startup_config():
    """配置初始化(必须最先跑): 建表 → 首启用 config.example.json 播种默认值(进初始化引导)。

    之后运行期只读 DB(引导/「通用」页写 DB, get_config 按 updated_at 热加载);
    导入失败只打日志不阻断启动(配置回退到 config.example.json 默认值, 服务仍可用)。
    """
    try:
        import sys
        from db.database import DB_PATH, init_db
        from lib.config import bootstrap
        init_db()
        state = bootstrap()
        print(f"[config] 配置初始化: {state}; DB={DB_PATH}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[config] 配置初始化失败(回退读文件): {e}", file=sys.stderr)
    try:
        from server.config import reload_config
        reload_config()
    except Exception:  # noqa: BLE001
        pass


def _startup_tools():
    """探测工具自检: NFO <fileinfo> 需要 mediainfo(本地) 与 ffprobe(WebDAV 通道)。

    缺了不阻断启动(有 mediainfo 兜底), 但必须在容器日志里点名 —— 否则 <fileinfo/>
    写空时根本看不出是镜像漏装还是网络不通。
    """
    try:
        import sys
        from lib.mediainfo import _find_cli, _ffprobe_cli
        mi, fp = _find_cli(), _ffprobe_cli()
        bits = [f"mediainfo={'ok:' + mi if mi else 'MISSING'}",
                f"ffprobe={'ok:' + fp if fp else 'MISSING(mediainfo 兜底仍可用)'}"]
        print(f"[tools] {' | '.join(bits)}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[tools] 探测工具自检失败: {e}", file=sys.stderr)


def _startup_cleanup():
    """把上次进程退出时还停在 running 的同步日志标记为中断(daemon 线程不保证收尾)。"""
    try:
        from datetime import datetime
        from db.database import SessionLocal, init_db
        from db.models import SyncLog
        init_db()
        s = SessionLocal()
        try:
            stuck = s.query(SyncLog).filter(SyncLog.status == "running").all()
            for log in stuck:
                log.status = "error"
                log.error = "中断于服务重启"
                log.finished_at = datetime.now()
            if stuck:
                s.commit()
        finally:
            s.close()
    except Exception:
        pass


def _startup_scheduler():
    """启动进程内调度器(定时: 近增 5 分钟 / 全量每日 3 点 / 可用性对账每日 5 点)。"""
    try:
        from server import scheduler
        scheduler.start()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """进程生命周期(替代已弃用的 @app.on_event("startup"))。

    先收尾上一轮遗留的 running 日志, 再起调度器 —— 反过来的话, 调度器首拍
    起的作业可能刚写 log 就被当成"中断"改掉。
    关停无需收尾: 调度器是 daemon 线程, 随进程退出。
    """
    _startup_config()
    _startup_tools()
    _startup_cleanup()
    _startup_scheduler()
    yield


app = FastAPI(title="MediaAuto Web", version="1.2", lifespan=lifespan)

# 浏览/搜索/缺失/详情全走本地 TMDB 缓存(缓存→TMDB API 现拉写库)。
app.include_router(search_router.router)
app.include_router(cd2_router.router)
app.include_router(config_router.router)   # 配置读写: GET/PUT /api/config + 引导
app.include_router(qbit_router.router)
app.include_router(sync_router.router)
app.include_router(jobs_router.router)   # 作业与缓存(/api/jobs、/api/cache)
app.include_router(browse_router.router)
app.include_router(nfo_router.router)    # NFO 更新: 读取上次更新时间 + 手动重新生成
app.include_router(track_router.router)
app.include_router(imgproxy_router.router)  # 图片本地缓存代理 /api/img/<token>


INDEX = PROJECT_ROOT / "server" / "static" / "index.html"
STATIC_DIR = PROJECT_ROOT / "server" / "static"


class LoginModel(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(data: LoginModel, response: Response, cfg: dict = Depends(get_config)):
    user, pwd = current_creds(cfg)
    if not data.username or not data.password or data.username != user or data.password != pwd:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = expected_token(cfg)  # 凭据匹配, 直接下发与期望一致的 token
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(request: Request, cfg: dict = Depends(get_config)):
    return {"authed": await is_authed(request, cfg)}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


# 前端静态资源禁缓存头: 改完即发, 否则手机浏览器启发式缓存旧版 css/js(改完看不到)
_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
async def index():
    return FileResponse(str(INDEX), headers=_NO_CACHE)


@app.get("/{path:path}")
async def static_assets(path: str):
    """前端静态资源: /css/*, /js/*, /icons/*, /sw.js, /manifest.webmanifest。
    2026-09 起前端按功能拆成多文件 + PWA(manifest/SW/图标), 统一按路径安全映射到
    server/static/ 下, 解析后的真实路径必须在 STATIC_DIR 内(防目录穿越); 同样禁缓存。"""
    if not path:
        return FileResponse(str(INDEX), headers=_NO_CACHE)
    full = (STATIC_DIR / path).resolve()
    # 防目录穿越: 解析后的真实路径必须仍在 STATIC_DIR 内
    if not str(full).startswith(str(STATIC_DIR.resolve())) or not full.is_file():
        raise HTTPException(404, "Not Found")
    return FileResponse(str(full), headers=_NO_CACHE)


if __name__ == "__main__":
    import uvicorn

    web = CONFIG.get("web", {})
    uvicorn.run(
        "server.main:app",
        host=web.get("host", "0.0.0.0"),
        port=int(web.get("port", 8787)),
        reload=False,
    )
