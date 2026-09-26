"""qBittorrent WebAPI 客户端 (基于 httpx 同步, 经 run_in_threadpool 调用)

封装 qBittorrent WebUI 的 HTTP API (https://github.com/qBittorrent/qBittorrent/wiki/WebUI-API):
  - 登录          POST /api/v2/auth/login   (表单 username/password, 返回 Set-Cookie SID)
  - 连接/全局统计  GET  /api/v2/app/webapiVersion + /api/v2/transfer/info
  - 添加磁力/种子  POST /api/v2/torrents/add  (urls + tags + save_path + category)
  - 任务列表      GET  /api/v2/torrents/info
  - 暂停/删除     POST /api/v2/torrents/pause | /torrents/delete

鉴权模型:
  - 登录 POST /api/v2/auth/login, 成功时拿到 Set-Cookie(SID), 后续请求带上该 Cookie 即可。
    cookie 名带端口(实测 `QBT_SID_9000`), 手动 curl 排查时注意。
  - ⚠️ **qBittorrent 5.2 起改用状态码表达结果**(commit qbittorrent/qBittorrent@7ddbf58;
    本机实测服务端 v5.2.0 / WebAPI 2.15.1):

      | 场景 | <= 5.1 | >= 5.2 |
      |---|---|---|
      | 登录成功 | 200 + 响应体 "Ok." | **204 No Content**(仍下发 Set-Cookie) |
      | 账号或密码错 | 200 + "Fails." | **401 Unauthorized** |
      | SID 失效 / 未登录 | 403 | 403(未变) |
      | 操作不存在的任务 | 200(静默通过) | **404 Not Found** |

    空响应类接口(pause / resume / delete …)也从"200 + 空体"变成 204。
    **所以不能只认 200** —— 否则会把成功的 204 当失败(2026-09-21 用户遇到的
    "登录失败: HTTP 204" 就是这个)。
  - SID 有有效期; 本模块进程内缓存一个 httpx.Client(带 cookie), 请求遇到 403 时
    自动重登一次再重试(免掉"过期就整个功能不可用"的手感)。
  - 所有请求 verify=False 兜底自签/明文内网场景; 如需严格校验可在 config 里留空走默认。

配置里的 qbit 关键字段:
  url       : WebAPI 地址(如 http://192.168.1.100:8080)。可带 /api/v2 前缀, 会被自动剥离归一。
  username  : WebUI 登录账号
  password  : WebUI 登录密码
  save_path : 可选。添加任务时的默认保存路径(留空 = qBittorrent 默认)。
  category  : 可选。默认分类(留空 = 默认分类)。
  timeout   : 单次请求超时秒数, 默认 20。

⚠️ 本客户端是【同步】的(httpx.Client)。FastAPI 异步路由里请用
   `await run_in_threadpool(qbit.list_torrents, cfg)` 调用, 不要直接在事件循环里阻塞。
"""
import os
import socket
import sys
import time
from urllib.parse import urlparse

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import httpx

DEFAULT_TIMEOUT = 20
# SID 缓存寿命: 到期强制重登(即便没遇到 403), 避免长期空闲后 SID 静默失效
_LOGIN_TTL = 1500  # 25 分钟


class QbitError(RuntimeError):
    """qBittorrent 调用失败(未配置 / 登录失败 / 请求失败)。"""


# ---------------------------------------------------------------------------
# 配置 / 连接
# ---------------------------------------------------------------------------
def _cfg(cfg):
    return cfg.get("qbit", {}) or {}


def _base_url(cfg):
    """归一化 WebAPI base(去掉末尾 / 与可能误写的 /api/v2 前缀)。"""
    q = _cfg(cfg)
    raw = (q.get("url") or "").strip()
    if not raw:
        raise QbitError("未配置 qbit.url(qBittorrent WebAPI 地址, 如 http://host:8080)")
    raw = raw.rstrip("/")
    # 容错: 用户可能把 /api/v2 也填进来了, 这里统一剥离, 调用方固定拼 /api/v2/...
    for suffix in ("/api/v2", "/api", "/webapi"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
    if not raw.lower().startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


def _cred(q):
    user = (q.get("username") or "").strip()
    pwd = q.get("password") or ""
    if not user:
        raise QbitError("未配置 qbit.username / qbit.password(qBittorrent WebUI 账号密码)")
    return user, pwd


def _is_private_host(url):
    """判断 base_url 是否指向【本地/内网】地址。

    内网地址(127.x / 10.x / 192.168.x / 172.16-31.x / 169.254.x / localhost /
    .local / .lan / 回环)必须【直连】, 不能走系统 HTTP 代理 —— 否则代理会把
    内网拨号搞超时(CD2 客户端踩过同样的坑)。公网域名则允许走代理。
    解析失败时保守返回 True(直连), 避免因代理导致内网连不上。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return True
    if not host:
        return True
    if host in ("localhost", "0.0.0.0") or host.endswith((".local", ".lan", ".localdomain")):
        return True
    try:
        ip = socket.gethostbyname(host)
    except Exception:  # noqa: BLE001  域名解析失败(多为内网主机名)→ 当内网直连
        return True
    parts = [int(x) for x in ip.split(".") if x.isdigit()]
    if len(parts) < 4:
        return True
    a, b = parts[0], parts[1]
    if a == 127 or a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 169 and b == 254:
        return True
    return False


# 进程内缓存的 (client, login_ts)。key 用 base_url, 不同 qbit 实例互不干扰。
_cached = {}


def _ok_body(r):
    """取"操作类"接口的响应文本; 2xx 空响应(5.2+ 的 204)归一成 "Ok."。

    qBittorrent <= 5.1 对这些接口返回 200 + "Ok."/空体, 5.2+ 返回 204 无响应体。
    直接 .text.strip() 会拿到 "" —— 上层(路由/前端)会误当成"没成功", 所以这里兜一下。
    """
    body = (r.text or "").strip()
    if body:
        return body
    return "Ok." if 200 <= r.status_code < 300 else body


def _do_login(client, base, user, pwd):
    """登录, 让 client 的 cookie jar 拿到 SID。

    ⚠️ qBittorrent 5.2 起登录成功返回 **204 No Content**(不再是 200 + "Ok."),
       失败返回 **401**(不再是 200 + "Fails.")。详见模块头部「鉴权模型」。
       所以这里按 2xx 判成功, 不能只认 200。
    """
    r = client.post(base + "/api/v2/auth/login",
                    data={"username": user, "password": pwd})
    sc = r.status_code
    if sc == 200:
        body = (r.text or "").strip().lower()
        if body.startswith("fails"):
            raise QbitError("qBittorrent 登录失败: 账号或密码错误")
        return                      # "Ok." 或空体(旧版成功)
    if 200 <= sc < 300:
        return                      # 204 = 5.2+ 登录成功
    if sc == 401:
        raise QbitError("qBittorrent 登录失败: 账号或密码错误 (HTTP 401)")
    if sc == 403:
        raise QbitError("qBittorrent 登录失败: IP 可能因多次失败被临时封禁"
                        "(HTTP 403), 重启 qBittorrent 或等待解封")
    raise QbitError(f"qBittorrent 登录失败: HTTP {sc} {(r.text or '')[:100]}")


def _client(cfg):
    """取(或建)一个已登录的 httpx.Client。带 cookie, 复用连接。"""
    q = _cfg(cfg)
    base = _base_url(cfg)
    user, pwd = _cred(q)
    hit = _cached.get(base)
    if hit is not None and (time.time() - hit[1]) < _LOGIN_TTL:
        return hit[0]
    # 清理旧的(可能已失效), 重建
    if hit is not None:
        try:
            hit[0].close()
        except Exception:  # noqa: BLE001
            pass
    # 内网地址直连(trust_env=False, 不走系统代理); 公网域名允许走代理
    client = httpx.Client(timeout=q.get("timeout") or DEFAULT_TIMEOUT, verify=False,
                          trust_env=not _is_private_host(base))
    try:
        _do_login(client, base, user, pwd)
    except httpx.HTTPError as e:
        # 网络层异常(超时/拒连/DNS)统一包成 QbitError: 否则上层只会看到干巴巴的
        # "timed out", 用户不知道是哪个地址不通(与 status() 的文档承诺一致)。
        client.close()
        raise QbitError(f"无法连接 qBittorrent({base}): {type(e).__name__}") from e
    except Exception:
        client.close()
        raise
    _cached[base] = (client, time.time())
    return client


def _request(cfg, method, path, **kw):
    """带"403 自动重登重试一次"的 HTTP 请求, 返回 httpx.Response(成功时)。"""
    q = _cfg(cfg)
    base = _base_url(cfg)
    user, pwd = _cred(q)
    client = _client(cfg)
    for attempt in (1, 2):
        try:
            r = client.request(method, base + path, **kw)
        except httpx.HTTPError as e:
            raise QbitError(f"qBittorrent 请求失败({base}{path}): "
                            f"{type(e).__name__}") from e
        if r.status_code == 403 and attempt == 1:
            # SID 失效: 重登一次再重试
            _do_login(client, base, user, pwd)
            _cached[base] = (client, time.time())
            continue
        if r.status_code in (401, 403):
            raise QbitError(f"qBittorrent 认证失败 (HTTP {r.status_code}), 请检查账号密码")
        if r.status_code == 404:
            # 5.2+ 对"操作不存在的任务"从静默 200 改成 404(qBittorrent@7ddbf58)
            raise QbitError(f"qBittorrent 找不到目标任务 (HTTP 404, {path}): "
                            "可能已被删除或 hash 无效")
        if r.status_code >= 400:
            raise QbitError(f"qBittorrent {path} 失败: HTTP {r.status_code} {_ok_body(r)[:160]}")
        return r
    raise QbitError("qBittorrent 请求失败: 反复 403")


# ---------------------------------------------------------------------------
# 状态 / 连接测试
# ---------------------------------------------------------------------------
def status(cfg):
    """连接测试 + 全局统计。

    返回:
      {
        "ok": True,
        "version": "v2.0.0",          # qBittorrent WebAPI 版本
        "app_name": "qBittorrent",    # (可选, 部分版本无)
        "transfer": {...}             # 全局传输统计(download_payload_rate 等)
      }
    失败抛 QbitError(未配置 / 不可达 / 登录失败)。
    """
    ver = _request(cfg, "GET", "/api/v2/app/webapiVersion")
    try:
        info = _request(cfg, "GET", "/api/v2/transfer/info")
        # 5.2+ 空响应可能是 204(无响应体), 直接 .json() 会抛
        transfer = info.json() if (info.content or b"").strip() else {}
    except Exception:  # noqa: BLE001  transfer/info 失败不致命, 版本能取到就算连上
        transfer = {}
    return {
        "ok": True,
        "version": (ver.text or "").strip(),
        "transfer": transfer,
    }


# ---------------------------------------------------------------------------
# 添加任务
# ---------------------------------------------------------------------------
def add_torrent(cfg, urls, tags="", save_path="", category=""):
    """添加一个(或换行分隔的多个)磁力/种子链接。

    urls      : 磁力链或 .torrent 直链。多个用 \n 分隔(与 CD2 同一套约定)。
    tags      : 逗号/空格分隔的标签。我们用 `ma:{kind}:{tmdb_id}` 标记来源作品,
                便于下载页按作品聚合"下到第几集"。
    save_path : 保存路径(留空 = 用 config.qbit.save_path, 再留空 = qBittorrent 默认)。
    category  : 分类(留空 = 用 config.qbit.category, 再留空 = 默认分类)。

    成功返回 qBittorrent 的响应文本(通常是 "Ok." 或 "Fails."); 抛 QbitError 表示失败。
    """
    q = _cfg(cfg)
    urls = (urls or "").strip()
    if not urls:
        raise QbitError("没有可添加的磁力/种子链接")
    data = {"urls": urls}
    if tags:
        data["tags"] = tags
    sp = (save_path or q.get("save_path") or "").strip()
    if sp:
        data["savepath"] = sp
    cat = (category or q.get("category") or "").strip()
    if cat:
        data["category"] = cat
    data["useAutoTMM"] = "true"
    r = _request(cfg, "POST", "/api/v2/torrents/add", data=data)
    # 成功返回 200 + "Ok."(旧版)或 204 空体(5.2+); 个别情况返回 "Fails." 但仍 2xx,
    # 所以这里不强行判 "Ok.", 交给调用方看返回文本(_ok_body 已把空体归一成 "Ok.")。
    return _ok_body(r)


# ---------------------------------------------------------------------------
# 任务列表 / 控制
# ---------------------------------------------------------------------------
def list_torrents(cfg, category="", tags="", state=""):
    """列出任务。可按 category / tags / state 过滤。

    返回 list[dict], 每项是 qBittorrent 原始任务对象(字段含:
      name, hash, state, size, progress, dlspeed, upspeed,
      num_seeds, num_leechs, added_on, tags, category, save_path, ratio ...)。
    为便于前端, 这里补两个派生字段:
      _progress : 0~100 整数
      _status   : 归一化中文状态(下载中/做种/暂停/出错/等待)
    """
    params = {}
    if category:
        params["category"] = category
    if tags:
        params["tags"] = tags
    if state:
        params["filter"] = state
    r = _request(cfg, "GET", "/api/v2/torrents/info", params=params)
    # 5.2+ 空结果可能返回 204(无响应体), .json() 会抛; 归一成空列表
    data = r.json() if (r.content or b"").strip() else []
    out = []
    for t in (data or []):
        t = dict(t)
        t["_progress"] = int(round(float(t.get("progress") or 0) * 100))
        zh, cat = norm_status(t.get("state"))
        t["_status"] = zh        # 中文状态(前端展示)
        t["_status_cat"] = cat   # 粗分类(下载中/做种/暂停/出错, 前端着色/统计)
        out.append(t)
    return out


def pause(cfg, hashes):
    if not hashes:
        return "Ok."
    h = hashes if isinstance(hashes, str) else ",".join(hashes)
    return _ok_body(_request(cfg, "POST", "/api/v2/torrents/pause", data={"hashes": h}))


def resume(cfg, hashes):
    if not hashes:
        return "Ok."
    h = hashes if isinstance(hashes, str) else ",".join(hashes)
    return _ok_body(_request(cfg, "POST", "/api/v2/torrents/resume", data={"hashes": h}))


def delete(cfg, hashes, delete_files=False):
    if not hashes:
        return "Ok."
    h = hashes if isinstance(hashes, str) else ",".join(hashes)
    data = {"hashes": h, "deleteFiles": "true" if delete_files else "false"}
    return _ok_body(_request(cfg, "POST", "/api/v2/torrents/delete", data=data))


# ---------------------------------------------------------------------------
# 状态归一化
# ---------------------------------------------------------------------------
# qBittorrent state 枚举 → 中文 + 粗分类。
# 粗分类(供前端着色/统计): downloading | seeding | paused | error | queued | unknown
_STATE_MAP = {
    # 下载中
    "downloading": ("下载中", "downloading"),
    "stalledDownloading": ("等待中", "queued"),
    "checkingDownloading": ("校验中", "downloading"),
    "allocating": ("分配中", "downloading"),
    "metaDL": ("元数据中", "downloading"),
    "queuedDownloading": ("排队下载", "queued"),
    "forcedDownloading": ("下载中", "downloading"),
    # 做种/上传
    "uploading": ("做种中", "seeding"),
    "stalledUploading": ("做种中", "seeding"),
    "queuedUploading": ("排队做种", "seeding"),
    "forcedUploading": ("做种中", "seeding"),
    "checkingUploading": ("校验中", "seeding"),
    # 暂停
    "pausedDownloading": ("已暂停", "paused"),
    "pausedUploading": ("已暂停", "paused"),
    # 出错
    "error": ("出错", "error"),
    "missingFiles": ("文件缺失", "error"),
    # 移动
    "moving": ("移动中", "unknown"),
    # 兜底
    "unknown": ("未知", "unknown"),
    "checking": ("校验中", "downloading"),
    "checkingResumeData": ("恢复中", "downloading"),
    "forcedMetaDL": ("元数据中", "downloading"),
}


def norm_status(state):
    """qBittorrent state → (中文, 粗分类)。未知 state 原样返回。"""
    s = str(state or "").strip()
    if s in _STATE_MAP:
        return _STATE_MAP[s]
    # 小写兜底(qBittorrent 个别版本大小写不一致)
    low = s.lower()
    if low in _STATE_MAP:
        return _STATE_MAP[low]
    return (s or "未知", "unknown")
