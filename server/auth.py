"""MediaAuto Web —— 登录与会话

本地媒体工具的轻量鉴权:用户名/密码来自数据库配置的 web.auth(可用环境变量
WEB_USER / WEB_PASS 覆盖)。登录成功后下发 HttpOnly Cookie,后续请求校验该 Cookie。
重启服务会刷新密钥,需要重新登录(本地工具可接受)。
"""
import hashlib
import hmac
import os
from fastapi import Cookie, Depends, HTTPException, Request

from server.config import get_config

COOKIE_NAME = "media_auto_session"
_SECRET = os.urandom(16).hex()


def get_secret():
    return _SECRET


def make_token(username: str, password: str, secret: str) -> str:
    return hashlib.sha256(f"{username}:{password}:{secret}".encode("utf-8")).hexdigest()


def expected_token(cfg: dict) -> str:
    auth = (cfg.get("web") or {}).get("auth") or {}
    username = os.environ.get("WEB_USER", auth.get("username", ""))
    password = os.environ.get("WEB_PASS", auth.get("password", ""))
    return make_token(username, password, _SECRET)


def current_creds(cfg: dict):
    auth = (cfg.get("web") or {}).get("auth") or {}
    return (
        os.environ.get("WEB_USER", auth.get("username", "")),
        os.environ.get("WEB_PASS", auth.get("password", "")),
    )


async def require_auth(
    request: Request,
    cfg: dict = Depends(get_config),
    session: str = Cookie(default="", alias=COOKIE_NAME),
):
    if not session:
        raise HTTPException(status_code=401, detail="未登录")
    if not hmac.compare_digest(session, expected_token(cfg)):
        raise HTTPException(status_code=401, detail="会话已失效,请重新登录")
    return True


async def is_authed(request: Request, cfg: dict = Depends(get_config)) -> bool:
    tok = request.cookies.get(COOKIE_NAME, "")
    if not tok:
        return False
    return hmac.compare_digest(tok, expected_token(cfg))
