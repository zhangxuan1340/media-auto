"""图片本地缓存代理 —— 省服务器带宽

前端开关「图片本地缓存」打开后, 封面图/演员图不再让浏览器直连 TMDB CDN,
而是走本代理 /api/img/<token> (token = base64url(上游图片 URL))。

省带宽策略(尊重 TMDB 的 Cache-Control):
  TMDB 图片**没有 ETag**, 但带 `Cache-Control: public, max-age≈1年` 和 `Last-Modified`。
  - **新鲜期(未超 max-age)**: 直接用磁盘缓存, **完全不请求上游**(最大省带宽点)。
  - **过期后**: 带 `If-Modified-Since` 问一次上游
      - 304(未变) → 仍是磁盘缓存, 只刷新新鲜期时间戳, **不重新下载**;
      - 200(变了) → 存新图(= "图片换了")。
  - 上游挂了 → 有旧缓存就降级返回旧缓存, 没有才 502。

缓存目录 data/img_cache/<sha1(上游URL)>.bin + 同名 .meta(存 last_modified/max_age/ts/ctype)。
TMDB 同步时若某作品 poster/backdrop 变了, 调 invalidate(url) 删旧缓存, 下次访问自动拉新图
(见 scripts/sync_tmdb.py)。
"""
import asyncio
import base64
import hashlib
import json
import os
import time
import weakref

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from lib.config import project_root
from lib import cache_stats as _cache_stats
from fastapi import Depends
from server.auth import require_auth

router = APIRouter(prefix="/api/img", tags=["imgproxy"],
                   dependencies=[Depends(require_auth)])

_CACHE_DIR = os.path.join(project_root(), "data", "img_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

_UPSTREAM_TIMEOUT = 30
_PROXY_PREFIXES = ("https://image.tmdb.org/", "http://image.tmdb.org/")
_UA = "MediaAuto/1.2 (image cache)"
# max-age 缺失/解析失败时的兜底新鲜期(秒)。TMDB 实测给 ~1 年。
_DEFAULT_MAX_AGE = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# 连接复用(2026-10 Too many open files 事故修复)
# ---------------------------------------------------------------------------
# 旧写法每个图片请求 `async with httpx.AsyncClient(...)` —— 上游( TMDB 图片 CDN) 一旦
# 出现 502 风暴, 高并发下瞬间开大量连接池 → fd 打满 → 连本地缓存文件都 open 不了
# (见崩溃栈 imgproxy.py _read_bytes 的 [Errno 24])。改为按事件循环复用**单例**, 并
# 限制连接池上限; 临时 loop 关闭时把孤儿池关掉, 不再泄漏。
_img_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _img_client():
    loop = asyncio.get_running_loop()
    c = _img_clients.get(loop)
    if c is None:
        c = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT, trust_env=False, follow_redirects=True,
                               limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
        _img_clients[loop] = c
        _watch_loop(loop, c)
    return c


def _watch_loop(loop, client):
    try:
        loop.add_closed_callback(lambda: _force_close(client))
    except Exception:  # noqa: BLE001
        pass


def _force_close(client):
    try:
        asyncio.run(client.aclose())
    except Exception:  # noqa: BLE001
        try:
            client._transport.close()   # 兜底: 同步关底层连接池, 直接释放套接字
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# token <-> url
# ---------------------------------------------------------------------------
def _enc(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _dec(token: str) -> str:
    pad = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "invalid image token")


def _key(url: str):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, h + ".bin"), os.path.join(_CACHE_DIR, h + ".meta")


def invalidate(url: str) -> bool:
    """删除某上游 URL 的本地缓存(同步时 poster 变了调它, 下次访问自动拉新图)。"""
    if not url:
        return False
    binp, metap = _key(url)
    removed = False
    for p in (binp, metap):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed = True
        except OSError:
            pass
    return removed


def _read_meta(binp: str):
    try:
        with open(binp + ".meta", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _write_meta(binp: str, meta: dict):
    try:
        with open(binp + ".meta", "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError:
        pass


def _read_bytes(binp: str):
    with open(binp, "rb") as f:
        return f.read()


def _parse_max_age(cache_control: str) -> int:
    for part in (cache_control or "").split(","):
        part = part.strip()
        if part.lower().startswith("max-age="):
            try:
                return int(part.split("=", 1)[1].strip().strip('"'))
            except (ValueError, IndexError):
                pass
    return _DEFAULT_MAX_AGE


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("")
async def img_config():
    """前端据此知道代理可用。开关本身在 /api/settings 的 image_cache 字段(可运行时切换)。"""
    return {"enabled": True, "prefixes": list(_PROXY_PREFIXES)}


@router.get("/{token}")
async def img(token: str):
    url = _dec(token)
    if not any(url.startswith(p) for p in _PROXY_PREFIXES):
        raise HTTPException(400, "仅支持代理 TMDB 图片")
    binp, _ = _key(url)
    meta = _read_meta(binp)
    now = time.time()

    # ---- 1) 新鲜期: 磁盘直出, 不碰上游(最大省带宽) ----
    if os.path.exists(binp) and meta:
        ts = meta.get("ts") or 0
        max_age = meta.get("max_age") or _DEFAULT_MAX_AGE
        if now - ts < max_age:
            _cache_stats.hit("image")
            return Response(_read_bytes(binp),
                            media_type=meta.get("ctype") or "image/jpeg",
                            headers={"X-ImgCache": "fresh"})

    # ---- 2) 过期/首取: 带上 If-Modified-Since 问上游 ----
    headers = {"User-Agent": _UA}
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]
    try:
        r = await _img_client().get(url, headers=headers)
    except Exception as e:  # noqa: BLE001
        if os.path.exists(binp):  # 上游挂 → 降级旧缓存
            _cache_stats.hit("image")
            return Response(_read_bytes(binp),
                            media_type=meta.get("ctype") or "image/jpeg",
                            headers={"X-ImgCache": "stale-fallback"})
        raise HTTPException(502, f"上游图片不可达: {e}")

    if r.status_code == 304:
        # 未更新 → 仍是磁盘缓存, 只把新鲜期时间戳续到当前, 不重新下载
        if os.path.exists(binp):
            _cache_stats.hit("image")
            _write_meta(binp, {**meta, "ts": now})
            return Response(_read_bytes(binp),
                            media_type=meta.get("ctype") or "image/jpeg",
                            headers={"X-ImgCache": "304-hit"})
        r = await _plain_get(url)  # 304 但本地没文件 → 兜底再拉
        if r is None:
            raise HTTPException(502, "上游 304 但本地无缓存")
        return _store(binp, r)

    if r.status_code != 200:
        if os.path.exists(binp):
            _cache_stats.hit("image")
            return Response(_read_bytes(binp),
                            media_type=meta.get("ctype") or "image/jpeg",
                            headers={"X-ImgCache": "stale-fallback"})
        raise HTTPException(502, f"上游 {r.status_code}")

    # ---- 3) 200(变了/首取) → 存新图 ----
    return _store(binp, r)


async def _plain_get(url: str):
    try:
        r = await _img_client().get(url, headers={"User-Agent": _UA})
        return r if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _store(binp: str, r: httpx.Response):
    """把刚回源拿到的图落盘 —— 只有真去上游下载了才会走到这里, 故记一次 miss。"""
    _cache_stats.miss("image")
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip() or "image/jpeg"
    data = r.content
    tmp = binp + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, binp)
    except OSError:
        pass
    _write_meta(binp, {
        "last_modified": r.headers.get("last-modified") or "",
        "max_age": _parse_max_age(r.headers.get("cache-control") or ""),
        "ctype": ctype,
        "ts": time.time(),
    })
    return Response(data, media_type=ctype, headers={"X-ImgCache": "store"})
