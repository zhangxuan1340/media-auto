#!/usr/bin/env python3
"""种子「内容预览截图」—— 第三方 whatslink.info 客户端 + 本地磁盘缓存

干什么: 磁力搜索列表里**点开大图弹窗**时, 去 whatslink.info 取这个种子的**内容预览截图**
        (Bitmagnet-Next-Web 详情页「内容预览」那几张 640×360 就是它)。它和 TMDB 海报 /
        Jackett 封面是**两条完全不同的图源**, 别混为一谈:
          · TMDB 海报 / Jackett 封面 = 作品的「封面」(竖版, 列表缩略图用)
          · whatslink 截图           = 种子文件的「内容截屏」(横版 16:9, 判断资源对不对)

接口(2026-10-07 实测):
  GET https://whatslink.info/api/v1/link?url=<urlencode("magnet:?xt=urn:btih:<40位hash>")>
  → {"error":"", "type":"FOLDER|FILE|UNKNOWN", "file_type":"folder|video|archive|",
     "name":"...", "size":320707857, "count":2,
     "screenshots":[{"time":0,"screenshot":"https://whatslink.info/image/<32hex>"}, …]}

  ⚠️ 必须传**带 `magnet:?xt=urn:btih:` 前缀的完整磁力**: 只传裸 40 位 hash 时接口照样
     返回 200, 但 `type=UNKNOWN` 且 0 张截图(不报错, 静默失败)。
  ⚠️ 单次 1.0~1.7s(偶发 7s), 且服务端基本不缓存 → **我方必须缓存**, 否则同一行反复点开
     就要反复等。
  ⚠️ 只有**含视频文件**的种子才有截图; 音乐/软件/电子书/归档 → 0 张(抽样命中约 1/3~1/2)。

缓存: data/whatslink_cache/<sha1(hash)>.json (含 ts/ttl/data)
  · 有截图 24h(内容稳定) · 无截图 6h(别反复问同一个空结果) · 上游失败 5min(让重试有机会)

网络: 直连优先(与 TMDB 客户端同口径, 不走环境代理); 直连失败再用环境代理重试一次 ——
      whatslink 是公网站点, 有代理/无代理两类环境都要能用(实测两种都通)。
"""
import asyncio
import hashlib
import json
import os
import random
import re
import time
import weakref

import httpx

from lib.config import project_root
from lib import cache_stats as _cache_stats

API_URL = "https://whatslink.info/api/v1/link"
_TTL_OK = 24 * 3600      # 有截图
_TTL_EMPTY = 6 * 3600    # 接口通了但没截图
_TTL_FAIL = 5 * 60       # 上游失败
_TIMEOUT = 12.0
_CLEAN_PROB = 0.02       # 写缓存时顺带做过期清理的概率
_UA = "MediaAuto/1.2 (magnet preview)"
_HASH_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
_MAGNET_RE = re.compile(r"urn:btih:([0-9a-fA-F]{40})", re.I)

_CACHE_DIR = os.path.join(project_root(), "data", "whatslink_cache")
try:
    os.makedirs(_CACHE_DIR, exist_ok=True)
except OSError:
    pass


def cache_dir():
    """缓存目录(供管理页「作业与缓存」统计用)。"""
    return _CACHE_DIR


# ---------------------------------------------------------------------------
# 连接复用(与 server/imgproxy.py 同款)
# ---------------------------------------------------------------------------
# 按 loop 存单例; 短命 loop 退出时由 `lib/loop_clients.py::run_coro` 调
# `aclose_loop_clients()` 关掉并 pop —— 不 pop 会因"client 经 transport 强引用 loop"
# 自引用而永不回收(fd 泄漏事故的根因, 见该模块 docstring)。
_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _client():
    loop = asyncio.get_running_loop()
    c = _clients.get(loop)
    if c is None:
        # trust_env=False → 直连优先(不走环境代理), 与 TMDB 客户端同口径
        c = httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False, follow_redirects=True,
                              headers={"User-Agent": _UA},
                              limits=httpx.Limits(max_connections=10, max_keepalive_connections=5))
        _clients[loop] = c
    return c


async def aclose_loop_clients():
    """关闭并移除【当前事件循环】名下的客户端(loop 结束前调用, 见 lib/loop_clients.py)。"""
    loop = asyncio.get_running_loop()
    c = _clients.pop(loop, None)
    if c is not None:
        try:
            await c.aclose()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# hash 归一化
# ---------------------------------------------------------------------------
def normalize_hash(value: str) -> str:
    """从「40 位 infoHash」或「含 urn:btih: 的磁力链」里取小写 hash; 取不到返回 ''。"""
    v = (value or "").strip()
    if not v:
        return ""
    m = _MAGNET_RE.search(v)
    if m:
        return m.group(1).lower()
    if _HASH_RE.match(v):
        return v.lower()
    return ""


def build_magnet(h: str) -> str:
    """⚠️ 必须带 `magnet:?xt=urn:btih:` 前缀 —— whatslink 用裸 hash 会返回 UNKNOWN/0 张。"""
    return "magnet:?xt=urn:btih:" + (h or "")


# ---------------------------------------------------------------------------
# 磁盘缓存
# ---------------------------------------------------------------------------
def _path(h: str) -> str:
    return os.path.join(_CACHE_DIR, hashlib.sha1(h.encode("utf-8")).hexdigest() + ".json")


def read_cache(h: str, now=None):
    """命中就返回归一化结果, 过期/不存在返回 None。"""
    try:
        with open(_path(h), "r", encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:  # noqa: BLE001  缺文件/坏 JSON 都当没有
        return None
    ttl = rec.get("ttl") or 0
    ts = rec.get("ts") or 0
    if ttl and (now or time.time()) - ts < ttl:
        return rec.get("data")
    return None


def write_cache(h: str, data: dict, ttl: int):
    p = _path(h)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "ttl": ttl, "data": data}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass
    # 惰性清理: 有小概率顺带扫一遍过期文件。这样目录不会随时间无限增长,
    # 又不必去动既有的每日缓存清理作业(那个专职清 img_cache)。
    if random.random() < _CLEAN_PROB:
        try:
            purge_expired()
        except Exception:  # noqa: BLE001
            pass


def purge_expired(now=None) -> int:
    """删除已过期的缓存文件, 返回删除个数。"""
    now = now or time.time()
    n = 0
    try:
        names = os.listdir(_CACHE_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(_CACHE_DIR, name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:  # noqa: BLE001  坏文件/半截写入 → 一并清掉
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        if now - (rec.get("ts") or 0) >= (rec.get("ttl") or 0):
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
    return n


# ---------------------------------------------------------------------------
# 上游
# ---------------------------------------------------------------------------
def _empty(reason: str, error: str = "") -> dict:
    return {"ok": False, "reason": reason, "error": error, "type": "", "file_type": "",
            "size": 0, "count": 0, "name": "", "screenshots": []}


def _normalize(payload: dict) -> dict:
    """上游 JSON → 归一化结果(只留我们要的字段, 顺带把截图地址摘出来)。"""
    shots = []
    for s in (payload.get("screenshots") or []):
        u = s.get("screenshot") if isinstance(s, dict) else None
        if u:
            shots.append(u)
    return {
        "ok": bool(shots),
        "reason": "" if shots else "no_screenshots",
        "error": "",
        "type": str(payload.get("type") or ""),
        "file_type": str(payload.get("file_type") or ""),
        "size": int(payload.get("size") or 0),
        "count": int(payload.get("count") or 0),
        "name": str(payload.get("name") or "")[:200],
        "screenshots": shots,
    }


async def _get_json(magnet: str):
    """请求上游。返回 (payload, err): 成功 payload 是 dict; 失败 payload=None, err=原因。"""
    errs = []
    # ① 直连(共享客户端, 复用连接)
    try:
        r = await _client().get(API_URL, params={"url": magnet})
        if r.status_code == 200:
            return r.json(), ""
        errs.append(f"直连 HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"直连 {e}")
    # ② 走环境代理重试一次(某些网络下公网站点需要代理)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, trust_env=True, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as c:
            r = await c.get(API_URL, params={"url": magnet})
        if r.status_code == 200:
            return r.json(), ""
        errs.append(f"代理 HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"代理 {e}")
    return None, "; ".join(errs)


async def _load(h: str):
    """真正打上游并决定缓存档位。返回 (data, ttl)。"""
    _cache_stats.miss("preview")
    payload, err = await _get_json(build_magnet(h))
    if payload is None:
        return _empty("upstream_error", err), _TTL_FAIL
    data = _normalize(payload)
    return data, (_TTL_OK if data["ok"] else _TTL_EMPTY)


# 同一 hash 并发只打一次上游(用户连点/关掉再开很常见); 值用完即 pop, 不留长命字典。
_locks: dict = {}


async def fetch(value: str, use_cache: bool = True) -> dict:
    """取某种子的内容预览截图。**永不抛错**(失败也返回带 reason 的 dict)。

    value: 40 位 infoHash, 或含 urn:btih: 的磁力链。
    """
    h = normalize_hash(value)
    if not h:
        return _empty("bad_hash")
    if use_cache:
        c = read_cache(h)
        if c is not None:
            _cache_stats.hit("preview")
            return c
    lock = _locks.setdefault(h, asyncio.Lock())
    try:
        async with lock:
            # 双检: 等锁期间别人可能已经写好缓存(省掉一次重复上游请求)
            if use_cache:
                c = read_cache(h)
                if c is not None:
                    _cache_stats.hit("preview")
                    return c
            data, ttl = await _load(h)
            # ⚠️ 写缓存必须在**锁内** —— 放锁外会让"双检"失效: 等待者进锁时结果还没落盘,
            #    于是同一个 hash 被并发打两次上游(实测验证脚本的并发用例会抓到)。
            write_cache(h, data, ttl)
    finally:
        _locks.pop(h, None)
    return data
