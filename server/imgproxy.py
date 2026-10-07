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
# 静态允许前缀: TMDB 图片 CDN。
_PROXY_PREFIXES = ("https://image.tmdb.org/", "http://image.tmdb.org/")
_UA = "MediaAuto/1.2 (image cache)"
# max-age 缺失/解析失败时的兜底新鲜期(秒)。TMDB 实测给 ~1 年。
_DEFAULT_MAX_AGE = 7 * 24 * 3600

# ---------------------------------------------------------------------------
# Jackett 封面代理(2026-10 新增)
# ---------------------------------------------------------------------------
# Jackett 的 Torznab 结果用 <torznab:attr name="coverurl"> 给封面, 但那个地址指向
# **Jackett 自身**(如 http://<jackett>/img/<indexer>/..), 常在内网/私有网段。浏览器
# 直连必然裂图(跨网不可达)。故由**服务端代取**: 后端把 coverurl 改写成
# /api/img/<token>(见 proxy_url), 前端 <img> 打同源代理, 代理再去 Jackett 取图。
#
# 允许前缀**从运行期配置 jackett.base 动态生成**, 绝不把内网地址写死进仓库;
# 且带 TTL 缓存(默认 5 分钟), 避免每个图片请求都读一次配置。
_JACKETT_TTL = 300.0
_jackett_cache = {"ts": 0.0, "prefixes": ()}


def _jackett_prefixes():
    """当前生效的 Jackett 允许前缀(http/https 两种, 末尾带 /)。带 TTL 缓存。"""
    now = time.monotonic()
    c = _jackett_cache
    if now - c["ts"] < _JACKETT_TTL:
        return c["prefixes"]
    prefixes = []
    try:
        from server.config import get_config  # 惰性: 避免启动期循环导入
        base = str(((get_config() or {}).get("jackett") or {}).get("base") or "").strip().rstrip("/")
        if base:
            # 同主机换协议的两种写法都放行(配置存 http 但站点跳 https 之类)
            for b in {base, base.replace("https://", "http://", 1),
                      base.replace("http://", "https://", 1)}:
                prefixes.append(b + "/")
    except Exception:  # noqa: BLE001  配置不可读 → 只放行 TMDB, 不影响主流程
        pass
    c["ts"] = now
    c["prefixes"] = tuple(prefixes)
    return c["prefixes"]


def _allowed_prefixes():
    return _PROXY_PREFIXES + _jackett_prefixes()


def _allowed(url: str) -> bool:
    return any(url.startswith(p) for p in _allowed_prefixes())


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
    return c


async def aclose_loop_clients():
    """关闭并移除【当前事件循环】名下的图片代理客户端 —— 必须在 loop 结束前调用。

    注意: imgproxy 的图片请求跑在**常驻的 server 主 loop** 上, 那个客户端故意常驻
    复用, 不应在此关闭; 本函数只会在短命 loop(若有)退出时被 `lib.loop_clients.run_coro`
    调用。`await aclose()` 释放套接字; `pop(loop)` 打破自引用避免泄漏(同 jellyfin/tmdb)。
    ⚠️ 旧版 `loop.add_closed_callback` 在 Python 3.12/3.13/3.14 上都不存在, 回调从没
    注册(被 `except: pass` 吞掉), 已弃用。
    """
    loop = asyncio.get_running_loop()
    c = _img_clients.pop(loop, None)
    if c is not None:
        try:
            await c.aclose()
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


def proxy_url(url: str) -> str:
    """把上游图片地址编码成本地代理路径(/api/img/<token>); 空串原样返回。

    供后端改写"浏览器不可直连"的图源(典型: Jackett 的 coverurl 指向内网 Jackett),
    前端拿到的就是同源相对路径, 无需知道上游主机。
    """
    if not url:
        return ""
    return "/api/img/" + _enc(url)


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
def _out(data: bytes, ctype, tag: str, max_age=None):
    """统一的图片响应: 带 X-ImgCache 诊断头 + Cache-Control。

    显式给 Cache-Control: private 让**浏览器自己**按 max-age 缓存 —— 这样 service
    worker 不必再替图片做一层缓存(见 sw.js 把 /api/img/ 列入 NEVER_CACHE),
    否则几十张缩略图会把 SW 的接口缓存额度(RUNTIME_MAX)挤爆。
    """
    try:
        ma = int(max_age or _DEFAULT_MAX_AGE)
    except (TypeError, ValueError):
        ma = _DEFAULT_MAX_AGE
    return Response(data, media_type=ctype or "image/jpeg",
                    headers={"X-ImgCache": tag,
                             "Cache-Control": f"private, max-age={ma}"})


@router.get("")
async def img_config():
    """前端据此知道代理可用。开关本身在 /api/settings 的 image_cache 字段(可运行时切换)。"""
    return {"enabled": True, "prefixes": list(_allowed_prefixes())}


@router.get("/{token}")
async def img(token: str):
    url = _dec(token)
    if not _allowed(url):
        raise HTTPException(400, "仅支持代理 TMDB / Jackett 图片")
    binp, _ = _key(url)
    meta = _read_meta(binp)
    now = time.time()

    # ---- 1) 新鲜期: 磁盘直出, 不碰上游(最大省带宽) ----
    if os.path.exists(binp) and meta:
        ts = meta.get("ts") or 0
        max_age = meta.get("max_age") or _DEFAULT_MAX_AGE
        if now - ts < max_age:
            _cache_stats.hit("image")
            return _out(_read_bytes(binp), meta.get("ctype"), "fresh", max_age)

    # ---- 2) 过期/首取: 带上 If-Modified-Since 问上游 ----
    headers = {"User-Agent": _UA}
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]
    try:
        r = await _img_client().get(url, headers=headers)
    except Exception as e:  # noqa: BLE001
        if os.path.exists(binp):  # 上游挂 → 降级旧缓存
            _cache_stats.hit("image")
            return _out(_read_bytes(binp), meta.get("ctype"), "stale-fallback", meta.get("max_age"))
        raise HTTPException(502, f"上游图片不可达: {e}")

    if r.status_code == 304:
        # 未更新 → 仍是磁盘缓存, 只把新鲜期时间戳续到当前, 不重新下载
        if os.path.exists(binp):
            _cache_stats.hit("image")
            _write_meta(binp, {**meta, "ts": now})
            return _out(_read_bytes(binp), meta.get("ctype"), "304-hit", meta.get("max_age"))
        r = await _plain_get(url)  # 304 但本地没文件 → 兜底再拉
        if r is None:
            raise HTTPException(502, "上游 304 但本地无缓存")
        return _store(binp, r)

    if r.status_code != 200:
        if os.path.exists(binp):
            _cache_stats.hit("image")
            return _out(_read_bytes(binp), meta.get("ctype"), "stale-fallback", meta.get("max_age"))
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
    max_age = _parse_max_age(r.headers.get("cache-control") or "")
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
        "max_age": max_age,
        "ctype": ctype,
        "ts": time.time(),
    })
    return _out(data, ctype, "store", max_age)
