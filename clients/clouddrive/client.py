#!/usr/bin/env python3
"""
CloudDrive2 gRPC 客户端 (纯 Python gRPC, 基于 grpcio —— 无需外部二进制)
====================================
CD2 的 API 是 gRPC。本模块用 **grpcio 原生 channel** 直连(不再 shell 出 grpcurl 子进程,
也就没有 "brew install grpcurl" 这类外部二进制依赖),封装常用操作:
  - 离线下载     (AddOfflineFiles)
  - 离线任务列表 (ListAllOfflineFiles 分页 / ListOfflineFilesByPath 按目录)
  - 离线配额     (GetOfflineQuotaInfo)
  - 列目录       (GetSubFiles, 服务端流式)
  - 移动 / 重命名 (MoveFile / RenameFile)

依赖: venv 内 `grpcio` + `grpcio-tools`(pip 安装, 见 requirements.txt)。
      Python 消息存根由官方 proto 预生成: clients/clouddrive/clouddrive_pb2*.py
      (proto 升级后重新生成: `venv/bin/python -m grpc_tools.protoc
        -I clients/clouddrive --python_out=clients/clouddrive
        --grpc_python_out=clients/clouddrive clouddrive.proto`,
       并把 pb2_grpc 里的 `import clouddrive_pb2` 改成带 try/except 的包内导入)。

proto 文件(clouddrive.proto)默认与本模块同目录(clients/clouddrive/),内容为
【CloudDrive2 官方 proto】。

--------------------------------------------------------------------------------
重要(踩坑记录,改代码前务必读)
--------------------------------------------------------------------------------
1) 目标地址格式: gRPC 的 target 只接受 host:port,不能带 http:// / https://。
   本模块会自动剥离协议头,并按协议头推断 use_tls。

2) 反代端口 ≠ gRPC 端口: CD2 的 Web UI 与原生 gRPC 默认同端口(19798)。
   若 Web UI 被 HTTPS 反代到别的端口(如 5002),那个端口提供的是 gRPC-web
   (HTTP/1.1 兼容格式),【不能被原生 gRPC 客户端直接使用】。
   远程访问必须做 TCP(L4)层转发,不能只做 HTTP 反代。

3) 必须同时提供 cloudName + cloudAccountId:
   ListAllOfflineFiles / GetOfflineQuotaInfo 都要求云盘名与账号 ID,
   缺任意一个都会报 `NotFound: cloud account xxx not found`。
   账号可由 GetSubFiles("/") 的每一项 `CloudAPI.name` / `CloudAPI.userName` 得到,
   本模块会自动发现,也可在 config 里用 cloud_name / cloud_account_id 显式指定。

4) 【性能】ListOfflineFilesByPath 在任务量大的账号上极慢:
   它是【一次性 unary 返回该目录下全部离线任务】。实测某账号 /Offline 下数千任务,
   单次调用耗时约 49 秒,客户端 30 秒超时必然失败(DeadlineExceeded)。
   => 轮询/查状态请用 list_offline_all() / find_offline()(分页,每页约 1 秒);
      确需按目录精确取用时才用 list_offline(path=...),并把 offline_by_path_timeout 放大。

5) 离线任务状态是枚举名(转 dict 后输出的是名字),值为:
   OFFLINE_INIT / OFFLINE_DOWNLOADING / OFFLINE_FINISHED / OFFLINE_ERROR / OFFLINE_UNKNOWN
   (注意不是老版本手写 proto 里的 Pending/Downloading/Finished/Error)

配置里的 clouddrive2 关键字段:
  host                 : 只写 host:port(如 192.168.1.100:19798)。兼容误写 https://host:port,会自动去协议。
  use_tls              : 目标是否 TLS。原生 gRPC 默认端口 19798 为明文 -> false;L4 转发到 TLS 时 true。
  insecure             : true 时跳过服务端证书校验(自签证书场景)。
  authority            : 可选,覆盖 :authority/SNI(用 IP 连但证书是域名时)。
  token                : 认证令牌,作为 authorization: Bearer 头发送。
  cloud_name           : 可选。云盘名(如 115open);不填则自动从 GetSubFiles("/") 发现。
  cloud_account_id     : 可选。云账号 ID(如 4978151,即 userName);不填则自动发现。
  timeout              : 可选。单次调用超时秒数,默认 30。
  offline_by_path_timeout : 可选。ListOfflineFilesByPath 的超时秒数,默认 180(该方法很慢)。
  offline_max_pages    : 可选。list_offline_all 默认最多翻多少页,每页 30 条,默认 5。
"""
import base64
import json
import os
import re
import sys
import threading

# 直接 `python clients/clouddrive/client.py` 跑 CLI 时,把项目根加进 sys.path,
# 保证 `from clients.clouddrive import ...` 可用(与 scripts/*.py 一致)。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict

from clients.clouddrive import clouddrive_pb2 as pb2
from clients.clouddrive import clouddrive_pb2_grpc as pb2g

DEFAULT_TIMEOUT = 30
PAGE_SIZE = 30  # CD2 ListAllOfflineFiles 固定每页 30 条


def _proto_default():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "clouddrive.proto")


def _resolve_proto(config, base_dir=None):
    """解析 proto 路径。

    - 未配置 proto_path: 默认取本模块同目录的 clouddrive.proto(官方 proto)。
    - 配置了 proto_path:
        * 绝对路径 -> 直接用;
        * 相对路径 -> 相对本模块目录解析(而非 cwd,避免启动目录不同导致找不到)。
    base_dir 保留为兼容参数(历史上用于相对项目根解析),当前仅在显式配置绝对路径时无关。
    """
    proto_rel = config.get("clouddrive2", {}).get("proto_path")
    if not proto_rel:
        return _proto_default()
    if os.path.isabs(proto_rel):
        return proto_rel
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), proto_rel)


def _split_hosts(raw):
    """把 host 配置拆成若干候选。

    支持这几种写法(逗号/分号/空白/换行分隔,越靠前越优先):
      "https://nas.example.com:5002"
      "http://192.168.1.100:19798"
      "http://localhost:19798"
      "https://nas.example.com:5002, http://192.168.1.100:19798, http://localhost:19798"
      也可以配 hosts: [ "...", "..." ](数组,优先于 host)
    """
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [p for p in re.split(r"[,;\s]+", str(raw or "")) if p.strip()]


def _resolve_target(cd2, raw=None):
    """规范化【单个】gRPC 目标并推断是否用 TLS。

    - host 允许写 `host:port`,也兼容写成 `https://host:port` / `http://host:port`:
      带协议头会被【自动去掉】——grpcurl 的 target 不接受协议头,否则会把它当主机名去拨号而失败
      (典型报错: Failed to dial target host "https://xxx": context deadline exceeded)。
    - use_tls: 显式配置优先;未配置时按协议头推断(https/grpcs/tls -> 启用 TLS,否则明文)。
      ⚠️ 注意: 一旦显式写了 use_tls,所有候选地址都按它处理,所以
      「外网 https + 内网明文」混用时不要写 use_tls,靠协议头自动推断。
    - insecure: 自签证书场景置 true(给 grpcurl 加 -insecure,跳过证书校验)。
      也会出现在任一候选为自签 https 时(避免外网反代证书链不全连不上)。
    - authority: 需覆盖 :authority/SNI 时填(例如用 IP 连、而证书是域名的场景)。
    返回 (target, use_tls, insecure, authority)。
    """
    raw = str(raw if raw is not None else (cd2.get("host") or "localhost:19798")).strip()
    scheme = ""
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
        scheme = scheme.lower()
    target = raw.strip().rstrip("/")

    # 协议头优先决定 TLS: 这样「外网 https + 内网 http」可以在同一个列表里混用,
    # 不会被一条全局 use_tls 覆盖掉(踩过的坑)。
    if scheme in ("https", "grpcs", "tls"):
        use_tls = True
    elif scheme in ("http", "grpc", "plaintext"):
        use_tls = False
    elif cd2.get("use_tls") is not None:
        use_tls = bool(cd2.get("use_tls"))
    else:
        use_tls = False

    return target, use_tls, bool(cd2.get("insecure", False)), (cd2.get("authority") or None)


def _target_candidates(cd2):
    """按优先级返回全部候选 (target, use_tls, insecure, authority)。

    典型配置(外网优先、内网回退):
      "host": "https://nas.example.com:5002, http://192.168.1.100:19798, http://localhost:19798"
    """
    raws = _split_hosts(cd2.get("hosts")) or _split_hosts(cd2.get("host")) or ["localhost:19798"]
    out, seen = [], set()
    for r in raws:
        item = _resolve_target(cd2, r)
        key = (item[0], item[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _clean_env():
    """返回一份【去掉 HTTP 代理】的环境变量(供需要 fork 子进程的调用方使用)。

    踩坑记录: 本机/某些 NAS 环境配了 http(s)_proxy。纯 Python gRPC(grpcio)
    实测: TLS 主地址(nas.example.com, 透明代理可正常 CONNECT 转发)与
    明文内网地址(172.16.x)在"有代理变量"的进程里都能连通 —— 所以本模块
    【不再全局清掉】os.environ 的代理变量(清掉会连带影响 Jellyfin/TMDB 等
    依赖系统代理的 httpx 客户端)。仅保留此函数给确需无代理子进程的场景。
    """
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        env.pop(k, None)
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    return env


# ---------------------------------------------------------------------------
# 底层调用
# ---------------------------------------------------------------------------
def _open_channel(target, use_tls, insecure, authority):
    """建一条到 target 的 gRPC channel。

    - 明文(host 写 http:// 或 grpc://): insecure_channel。
    - TLS(host 写 https:// 或 grpcs://): ssl_channel_credentials()。
      * authority 非空时覆盖 :authority/SNI(用 IP 连而证书是域名)。
      * insecure=True 表示"自签/不校验证书"。grpcio 纯 Python 没有一键 skip-verify,
        这里退化为标准 TLS;若证书真的不被信任,候选回退会切到明文内网地址
        (172.16.x:19798 / localhost:19798)兜底。当前部署用 Let's Encrypt 合法证书,
        标准 TLS 直接可用。
    """
    opts = []
    if authority:
        opts.append(("grpc.ssl_target_name_override", authority))
    if use_tls:
        return grpc.secure_channel(target, grpc.ssl_channel_credentials(), options=opts)
    return grpc.insecure_channel(target, options=opts)


# ── channel 复用(2026-10-02 性能修复)──────────────────────────────────────
# 旧写法每次调用 _open_channel 建一条 channel、finally 里立刻 close —— 每次都要
# TCP(+TLS) 握手, 下载页/离线列表一次轮询里几十个方法调用就是几十次握手。
# grpc.Channel 本身线程安全、自带连接管理与自动重连, 进程内按 (target, 参数) 缓存复用;
# 拨号类失败(unavailable/超时)才把它丢掉重建, 避免坏连接被永久缓存。
_CHANNELS: dict = {}
_CHANNELS_LOCK = threading.Lock()


def _get_channel(key, target, use_tls, insecure, authority):
    with _CHANNELS_LOCK:
        ch = _CHANNELS.get(key)
        if ch is None:
            ch = _open_channel(target, use_tls, insecure, authority)
            _CHANNELS[key] = ch
        return ch


def _drop_channel(key):
    with _CHANNELS_LOCK:
        ch = _CHANNELS.pop(key, None)
    if ch is not None:
        try:
            ch.close()
        except Exception:  # noqa: BLE001
            pass



def _method_info(svc, method):
    """从 proto 描述符取某方法的 (请求消息类, 是否服务端流式)。

    用描述符而不是硬编码列表 —— proto 升级后无需改这里。
    """
    svc_desc = pb2.DESCRIPTOR.services_by_name[svc]
    m = svc_desc.methods_by_name[method]
    return m.input_type._concrete_class, bool(m.server_streaming)


def _msg_to_dict(msg, emit_defaults):
    """protobuf 消息 → 与 grpcurl protojson 兼容的 dict。

    - preserving_proto_field_name=True: 字段名保持 proto 原名(fullPathName/isDirectory/...),
      不用 camelCase,上层代码依赖这些名字。
    - always_print_fields_with_no_presence=emit_defaults: 等价 grpcurl 的 -emit-defaults,
      零值标量(bool/int/enum/bytes)也打印,保证 isDirectory=false 等字段恒在。
    - 枚举默认打印【名字】(OFFLINE_FINISHED 等),与 grpcurl 一致。
    """
    return MessageToDict(msg, preserving_proto_field_name=True,
                         always_print_fields_with_no_presence=emit_defaults)


def _grpcurl(config, method, data=None, auth=True, emit_defaults=False,
             timeout=None, base_dir=None):
    """调用一个 CD2 gRPC 方法(纯 Python gRPC, 无需 grpcurl 二进制),返回解析后的 JSON。

    host 可配多个候选(见 _target_candidates),拨号/不可用类失败会自动尝试下一个,
    这样「外网 https://nas...:5002 优先、内网 http://172.16.x:19798 兜底」能自动容错。

    返回: 无响应 -> None;单条 -> dict;流式多条 -> list[dict]。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    candidates = _target_candidates(cd2)
    svc = cd2.get("grpc_service", "CloudDriveFileSrv")
    if timeout is None:
        timeout = cd2.get("timeout") or DEFAULT_TIMEOUT
    token = (cd2.get("token") or "").strip()
    meta = [("authorization", f"Bearer {token}")] if (auth and token) else []

    try:
        req_cls, is_stream = _method_info(svc, method)
    except KeyError as e:
        raise RuntimeError(f"proto 服务 {svc} 里没有方法 {method}(检查 clouddrive2 配置/ proto 是否最新)") from e
    stub_cls = getattr(pb2g, f"{svc}Stub", None)
    if stub_cls is None:
        raise RuntimeError(f"没有生成 {svc} 的 gRPC stub(重新编译 clouddrive.proto)")

    last_err = None
    for idx, (target, use_tls, insecure, authority) in enumerate(candidates):
        key = (target, use_tls, insecure, authority)
        try:
            ch = _get_channel(key, target, use_tls, insecure, authority)  # 复用, 不再每调用建关
            stub = stub_cls(ch)
            rpc = getattr(stub, method)
            req = ParseDict(data, req_cls()) if data else req_cls()

            if is_stream:
                msgs = [_msg_to_dict(m, emit_defaults)
                        for m in rpc(req, metadata=meta, timeout=timeout)]
            else:
                resp = rpc(req, metadata=meta, timeout=timeout)
                msgs = [_msg_to_dict(resp, emit_defaults)] if resp is not None else []

            msgs = [m for m in msgs if m]  # 过滤全空消息
            if not msgs:
                return None
            return msgs[0] if len(msgs) == 1 else msgs

        except grpc.RpcError as e:
            code = e.code()
            detail = e.details()
            err = f"{code.name}: {detail}"
            low = detail.lower()
            # 拨号/不可用/超时类 → 换下一个候选(外网慢、内网快是常见情形)
            dial_fail = code in (grpc.StatusCode.UNAVAILABLE,
                                 grpc.StatusCode.DEADLINE_EXCEEDED,
                                 grpc.StatusCode.UNAUTHENTICATED) or any(
                                     k in low for k in ("unavailable", "deadline",
                                                        "refused", "timeout", "no route"))
            extra = ""
            if method == "ListOfflineFilesByPath" and code == grpc.StatusCode.DEADLINE_EXCEEDED:
                extra = ("  (该方法需一次性返回目录下全部离线任务,账号任务多时会非常慢;"
                         "轮询请改用 list_offline_all()/find_offline(),或调大 "
                         "clouddrive2.offline_by_path_timeout)")
            if dial_fail and idx + 1 < len(candidates):
                _drop_channel(key)   # 这条候选拨不通 → 丢掉缓存的 channel,下次重建
                last_err = f"CD2 {method} @ {target} 失败: {err}"
                continue
            hints = []
            if dial_fail:
                hints.append("1) 确认该端口从本机可达: nc -vz <host> <port>; "
                             "2) TLS 目标 host 写成 https://host:port(或 use_tls=true);"
                             "3) 原生 gRPC 默认 19798 明文,若连的是纯 HTTP 反代的 Web UI 端口,"
                             "需在反代做 TCP(L4)/grpc_pass 转发; "
                             "4) 可把 host 配成多个候选自动回退")
            if "cloud account" in low and "not found" in low:
                hints.append("云账号缺失/错误: 请确认 管理 → 通用 → CloudDrive2(clouddrive2) 的 "
                             "cloud_name + cloud_account_id(账号可由 GetSubFiles(\"/\") 的 "
                             "CloudAPI.name / userName 得到)")
            raise RuntimeError(f"CD2 {method} 失败: {err}"
                               + (("\n  排查: " + "; ".join(hints)) if hints else ""))
        except Exception as e:  # noqa: BLE001
            last_err = f"CD2 {method} @ {target} 异常: {e}"
            if idx + 1 < len(candidates):
                _drop_channel(key)   # 换候选前把这条的缓存 channel 丢掉(可能就是它坏的)
                continue
            raise
        # 注意: 这里**不再 close** channel —— channel 按 (target, 参数) 缓存复用
        # (见 _get_channel), close 会把复用的连接一起砍掉。grpc.Channel 自带
        # 连接管理/自动重连, 进程存活期内复用即可。

    raise RuntimeError(last_err or f"CD2 {method} 失败: 所有候选地址均不可用 {candidates}")


# ---------------------------------------------------------------------------
# 业务封装
# ---------------------------------------------------------------------------
def get_token(config, username, password, totp=None, base_dir=None):
    data = {"username": username, "password": password}
    if totp:
        data["totpCode"] = totp
    return _grpcurl(config, "GetToken", data, auth=False, base_dir=base_dir)


# ===========================================================================
# 离线链接分隔规则(极其重要,搞错会导致"多个链接合并成一个"而失败)
# ---------------------------------------------------------------------------
# CD2 的 AddOfflineFileRequest.urls 是【单个 string 字段】(proto 字段 1,非 repeated)。
# 多链接必须塞进同一个字符串,且用【换行 \n】分隔;CD2 按 \n 拆成多个离线任务。
#   ❌ 绝不能用逗号/空格/分号拼接 —— 整串会被当成一个链接,符号混入磁力链 → 失败
#   ❌ 也不能依赖 CD2 的批量语义去冒险 —— 所以默认【一次调用只推一个链接】最稳
# 因此本层提供两种入口:
#   add_offline()      —— 推【单个】链接,一次调用一个(默认,杜绝合并)
#   add_offline_batch()—— 批量推,内部统一用 \n 连接(仅在确认 CD2 接受换行时使用)
# ===========================================================================
_URL_PREFIXES = (
    "magnet:", "http://", "https://", "ed2k://",
    "thunder://", "ftp://", "ftps://",
)


def is_valid_url(s):
    """判断是否是 CD2 能识别的离线链接(磁力/直链/ed2k/迅雷/ftp)。"""
    s = (s or "").strip()
    if not s:
        return False
    return s.lower().startswith(_URL_PREFIXES)


def split_urls(raw):
    """把"可能含换行/逗号/分号/空格"的链接文本(或它们的列表)拆成干净的链接列表。
    入口可能是字符串,也可能是若干"原始片段"组成的列表(如 --magnet 重复传 + 文件内容),
    每个片段内部同样可能混排多链接,因此【每个片段都要拆】,不能信任列表元素已干净。
    磁力链本身不含这些符号(已 URL 编码),按它们拆分是安全的。
    返回去重、去空后的列表。"""
    if isinstance(raw, (list, tuple)):
        chunks = [str(x) for x in raw]
    else:
        chunks = [raw or ""]
    items = []
    for chunk in chunks:
        # 优先换行,其次逗号/分号,最后空白;磁力链之间不会跨这些符号。
        parts = re.split(r"[\r\n]+|,|;|\s+", chunk)
        items.extend(p for p in parts if p and p.strip())
    out, seen = [], set()
    for it in items:
        it = it.strip()
        if not it:
            continue
        key = it.lower()
        if key in seen:  # 按 infoHash 去重,避免重复推同一资源
            continue
        seen.add(key)
        out.append(it)
    return out


def _ensure_ok(res, what):
    """CD2 的写操作返回 FileOperationResult{success,errorMessage}。

    注意: protojson 默认【省略零值】,所以 success=false 时该字段可能根本不出现,
    靠 `res.get("success")` 为 None 来判定失败并不可靠。这里做双保险:
      1) 本模块对这些写操作统一加了 -emit-defaults,所以正常会看到 success=false;
      2) 仍然兜底: 若 success 缺失但 errorMessage 非空,也判为失败。
    失败一律升级为异常,调用方按异常处理,避免"静默失败当成功"。
    """
    if not isinstance(res, dict):
        return res
    if res.get("success") is False:
        raise RuntimeError(f"{what} 失败: {res.get('errorMessage') or res}")
    if res.get("success") is None and (res.get("errorMessage") or "").strip():
        raise RuntimeError(f"{what} 失败: {res.get('errorMessage')}")
    return res


def add_offline(config, url, to_folder, base_dir=None):
    """推【单个】离线链接:一次 AddOfflineFiles 调用只放一个链接,从根本上杜绝合并。"""
    if not is_valid_url(url):
        raise ValueError(f"非法离线链接(已忽略): {str(url)[:80]}")
    data = {"urls": url.strip(), "toFolder": to_folder}
    res = _grpcurl(config, "AddOfflineFiles", data, emit_defaults=True, base_dir=base_dir)
    return _ensure_ok(res, "AddOfflineFiles")


def add_offline_batch(config, urls, to_folder, base_dir=None):
    """批量推:多链接用 \\n 连接后一次调用。仅在你已验证 CD2 接受换行分隔时使用。"""
    clean = split_urls(urls)
    if not clean:
        raise ValueError("没有有效的离线链接")
    bad = [u for u in clean if not is_valid_url(u)]
    if bad:
        raise ValueError("检测到非法链接: " + " | ".join(u[:40] for u in bad[:3]))
    data = {"urls": "\n".join(clean), "toFolder": to_folder}
    res = _grpcurl(config, "AddOfflineFiles", data, emit_defaults=True, base_dir=base_dir)
    return _ensure_ok(res, "AddOfflineFiles(批量)")


# ---------------------------------------------------------------------------
# 云账号发现(离线接口必须带 cloudName + cloudAccountId)
# ---------------------------------------------------------------------------
def list_cloud_accounts(config, base_dir=None):
    """从 GetSubFiles("/") 的每一项里收集云盘名 + 账号 ID。

    返回 [{"cloud": "115open", "account": "4978151"}, ...](去重、保序)。
    取的是 CloudDriveFile.CloudAPI.name / CloudAPI.userName。
    """
    files = get_subfiles(config, "/", base_dir=base_dir)
    out, seen = [], set()
    for f in files:
        api = f.get("CloudAPI") or {}
        name = api.get("name")
        acct = api.get("userName") or api.get("nickName")
        if name and acct and (name, acct) not in seen:
            seen.add((name, acct))
            out.append({"cloud": name, "account": acct})
    return out


def resolve_account(config, base_dir=None):
    """得到 (cloud_name, cloud_account_id)。

    优先用配置里的 clouddrive2.cloud_name / cloud_account_id(显式配置最稳);
    未配置则自动从 GetSubFiles("/") 发现。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    name = (cd2.get("cloud_name") or "").strip()
    acct = str(cd2.get("cloud_account_id") or "").strip()
    if name and acct:
        return name, acct
    accounts = list_cloud_accounts(config, base_dir=base_dir)
    if not accounts:
        raise RuntimeError(
            "未发现任何云盘账号。请在 管理 → 通用 → CloudDrive2(clouddrive2) 里显式配置 "
            "cloud_name 与 cloud_account_id(可从 CD2 网页或 GetSubFiles(\"/\") 的 "
            "CloudAPI.name / userName 得到)"
        )
    return accounts[0]["cloud"], accounts[0]["account"]


# ---------------------------------------------------------------------------
# 离线任务:列表 / 查询 / 配额
# ---------------------------------------------------------------------------
def list_offline_all(config, max_pages=None, status=None, base_dir=None, timeout=None):
    """账户级【分页】列出离线任务(推荐)。按 addTime 倒序(最新在最前)。

    - max_pages: 最多翻多少页(每页 30 条),默认取 config.clouddrive2.offline_max_pages 或 5。
                 None 表示一直翻到最后一页(大账号可能上百页,较慢)。
    - status   : 可选,只保留指定状态(如 "OFFLINE_FINISHED"/"OFFLINE_ERROR"),
                 也接受 is_/状态判定函数不方便时直接传字符串。
    返回 list[dict],每项含 name/size/url/status/infoHash/fileId/addTime/parentId/percendDone/peers。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    cloud, acct = resolve_account(config, base_dir=base_dir)
    if max_pages is None:
        max_pages = int(cd2.get("offline_max_pages") or 5)

    out = []
    page = 1
    while True:
        res = _grpcurl(
            config, "ListAllOfflineFiles",
            {"cloudName": cloud, "cloudAccountId": acct, "page": page},
            emit_defaults=True, timeout=timeout, base_dir=base_dir,
        )
        if not res:
            break
        files = res.get("offlineFiles") or []
        if not files:
            break
        out.extend(files)
        page_count = res.get("pageCount") or 0
        if page >= page_count or page >= max_pages:
            break
        page += 1

    if status:
        want = str(status).upper()
        out = [f for f in out if str(f.get("status", "")).upper() == want]
    return out


def list_offline_by_path(config, path, base_dir=None, timeout=None):
    """按目录列出离线任务(ListOfflineFilesByPath)。

    ⚠️ 性能警告: 该方法是 unary 且一次性返回该目录下【全部】任务。任务多时极慢
    (实测某账号 /Offline 下数千任务需约 49 秒)。仅在你确实需要按目录精确取用时调用,
    并把 clouddrive2.offline_by_path_timeout(默认 180s)放大。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    if timeout is None:
        timeout = int(cd2.get("offline_by_path_timeout") or 180)
    res = _grpcurl(config, "ListOfflineFilesByPath", {"path": path},
                   emit_defaults=True, timeout=timeout, base_dir=base_dir)
    if not res:
        return []
    return res.get("offlineFiles") or []


def list_offline(config, path=None, base_dir=None, max_pages=None, timeout=None):
    """兼容入口,返回离线任务列表。

    - 不给 path: 走 list_offline_all()(分页,快,推荐)。
    - 给了 path: 走 list_offline_by_path()(按目录精确,但可能很慢)。
      若按目录查询失败/超时,自动降级为账户级分页列表(保证不中断调用方)。
    """
    if not path:
        return list_offline_all(config, max_pages=max_pages, base_dir=base_dir, timeout=timeout)
    try:
        return list_offline_by_path(config, path, base_dir=base_dir, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        print(f"  ! ListOfflineFilesByPath({path}) 失败,降级为账户级分页列表: {e}")
        return list_offline_all(config, max_pages=max_pages, base_dir=base_dir, timeout=timeout)


def find_offline(config, info_hash=None, name_contains=None, max_pages=None,
                 base_dir=None, timeout=None):
    """在账户级离线任务里【从最新往后】找一条匹配的任务,找到即返回 dict,否则 None。

    列表按 addTime 倒序,新推的任务总在第 1 页,所以轮询查状态开销很小。
    匹配规则: 优先 infoHash 精确相等(忽略大小写);否则用 name_contains 做子串匹配。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    if max_pages is None:
        max_pages = int(cd2.get("offline_max_pages") or 5)
    ih = (info_hash or "").strip().lower()
    nc = (name_contains or "").strip().lower()

    cloud, acct = resolve_account(config, base_dir=base_dir)
    page = 1
    while True:
        res = _grpcurl(
            config, "ListAllOfflineFiles",
            {"cloudName": cloud, "cloudAccountId": acct, "page": page},
            emit_defaults=True, timeout=timeout, base_dir=base_dir,
        )
        if not res:
            break
        files = res.get("offlineFiles") or []
        if not files:
            break
        for f in files:
            if ih and str(f.get("infoHash", "")).strip().lower() == ih:
                return f
            if nc and nc in str(f.get("name", "")).lower():
                return f
        if page >= (res.get("pageCount") or 0) or page >= max_pages:
            break
        page += 1
    return None


def get_offline_quota(config, base_dir=None, timeout=None):
    """返回离线配额 {'total','used','left'}(均为整数)。"""
    cloud, acct = resolve_account(config, base_dir=base_dir)
    res = _grpcurl(config, "GetOfflineQuotaInfo",
                   {"cloudName": cloud, "cloudAccountId": acct},
                   emit_defaults=True, timeout=timeout, base_dir=base_dir)
    if not res:
        return {"total": 0, "used": 0, "left": 0}
    return {
        "total": int(res.get("total") or 0),
        "used": int(res.get("used") or 0),
        "left": int(res.get("left") or 0),
    }


def get_offline_stats(config, base_dir=None, timeout=None):
    """离线任务总览: 总数 / 各状态计数 / 配额(一次账户级分页扫描)。"""
    files = list_offline_all(config, max_pages=1, base_dir=base_dir, timeout=timeout)
    counts = {}
    for f in files:
        counts[str(f.get("status", "UNKNOWN"))] = counts.get(str(f.get("status", "UNKNOWN")), 0) + 1
    try:
        quota = get_offline_quota(config, base_dir=base_dir, timeout=timeout)
    except Exception:  # noqa: BLE001
        quota = {"total": 0, "used": 0, "left": 0}
    return {"sampled": len(files), "counts": counts, "quota": quota}


# ---------------------------------------------------------------------------
# 目录 / 文件操作
# ---------------------------------------------------------------------------
def get_subfiles(config, path, base_dir=None, timeout=None):
    """返回 path 下的目录内容(CloudDriveFile 列表)。

    官方签名是 `GetSubFiles(ListSubFileRequest) -> stream SubFilesReply`,每条流消息是
    `{"subFiles": [CloudDriveFile, ...]}`;这里把若干条流消息里的 subFiles 拍平成一个列表。
    判定目录/文件请用 `isDirectory`(bool),不要用 fileType 的数值 —— protojson 输出的是枚举名。
    """
    res = _grpcurl(config, "GetSubFiles", {"path": path}, emit_defaults=True,
                   timeout=timeout, base_dir=base_dir)
    if not res:
        return []
    msgs = res if isinstance(res, list) else [res]
    files = []
    for m in msgs:
        if isinstance(m, dict):
            files.extend(m.get("subFiles") or [])
    return files


def is_dir(f):
    """CloudDriveFile 是否目录。优先看 isDirectory,回退到 fileType 名称。"""
    if not isinstance(f, dict):
        return False
    if "isDirectory" in f:
        return bool(f.get("isDirectory"))
    ft = str(f.get("fileType") or "").lower()
    return ft in ("directory", "0")


def find_file_by_path(config, parent_path, name, base_dir=None, timeout=None):
    """在 parent_path 下按名字找一项,返回 CloudDriveFile 或 None(拿 id 用)。"""
    for f in get_subfiles(config, parent_path, base_dir=base_dir, timeout=timeout):
        if f.get("name") == name:
            return f
    return None


def move_file(config, paths, dest, conflict="Rename", move_across_clouds=True,
              handle_conflict_recursively=None, base_dir=None):
    """移动文件/目录到 dest 目录。

    conflict: "Overwrite" | "Rename" | "Skip"(同名冲突策略)。
    handle_conflict_recursively: True 时对「目录 vs 目录」冲突做递归处理
      (即合并同名目录、只补齐缺的文件),用于"同剧补季"场景。
    """
    if isinstance(paths, str):
        paths = [paths]
    data = {
        "theFilePaths": paths,
        "destPath": dest,
        "conflictPolicy": conflict,
        "moveAcrossClouds": move_across_clouds,
    }
    if handle_conflict_recursively is not None:
        data["handleConflictRecursively"] = handle_conflict_recursively
    res = _grpcurl(config, "MoveFile", data, emit_defaults=True, base_dir=base_dir)
    return _ensure_ok(res, f"MoveFile {paths} -> {dest}")


def rename_file(config, path, new_name, base_dir=None):
    data = {"theFilePath": path, "newName": new_name}
    res = _grpcurl(config, "RenameFile", data, emit_defaults=True, base_dir=base_dir)
    return _ensure_ok(res, f"RenameFile {path} -> {new_name}")


def no_delete_roots(config):
    """返回【禁止删除】的路径前缀(默认取 organize.cloud_root,即 /Cloud)。

    用户约定: /Cloud 是媒体最终存放位置,只允许往里移动,不允许删任何东西。
    配置: clouddrive2.no_delete_paths(数组或字符串),留空则用 /Cloud。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    roots = cd2.get("no_delete_paths")
    if roots in (None, "", []):
        org = config.get("organize") or {}
        roots = [org.get("cloud_root") or "/Cloud"]
    if isinstance(roots, str):
        roots = [roots]
    out = []
    for r in roots:
        r = str(r or "").strip().rstrip("/")
        if r and r != "/":
            out.append(r)
    return out


def is_under(path, root):
    """判断 path 是否就是 root 或在 root 之下(按路径段比较,避免 /Cloudx 误判)。"""
    p = "/" + str(path or "").strip("/")
    r = "/" + str(root or "").strip("/")
    return p == r or p.startswith(r + "/")


def delete_files(config, paths, permanent=False, base_dir=None, force=False):
    """删除文件/目录(默认进回收站,可恢复)。

    permanent=True 时调 DeleteFilesPermanently —— 不可恢复,慎用。
    DeleteFiles 是 MultiFileRequest{repeated string path},一次可删多个。

    ⚠️ 保护规则: /Cloud(organize.cloud_root)下【一律禁止删除】(用户约定只允许往里移动)。
    命中会直接抛 PermissionError;确实需要时可显式传 force=True 绕过。
    """
    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in paths if p]
    if not paths:
        return {"success": True, "resultFilePaths": []}

    roots = no_delete_roots(config)
    blocked = [p for p in paths if any(is_under(p, r) for r in roots)]
    if blocked and not force:
        raise PermissionError(
            f"拒绝删除受保护路径(只允许往里移动,不允许删除): {blocked[:3]}"
            + (f" …共 {len(blocked)} 个" if len(blocked) > 3 else "")
            + f";受保护根 = {roots}(可在 config 的 clouddrive2.no_delete_paths 调整)"
        )

    method = "DeleteFilesPermanently" if permanent else "DeleteFiles"
    res = _grpcurl(config, method, {"path": paths}, emit_defaults=True, base_dir=base_dir)
    return _ensure_ok(res, f"{method} {paths[:3]}{'…' if len(paths) > 3 else ''}")


def delete_file(config, path, permanent=False, base_dir=None, force=False):
    """删除单个文件/目录。"""
    return delete_files(config, [path], permanent=permanent, base_dir=base_dir, force=force)


def create_folder(config, parent_path, folder_name, base_dir=None):
    """在 parent_path 下建目录,返回新建的 CloudDriveFile(folderCreated)。已存在时返回 None。

    CreateFolderResult{folderCreated, result},失败信息在嵌套的 result 里,
    所以这里对 res["result"] 再做一次 _ensure_ok。
    """
    res = _grpcurl(config, "CreateFolder", {"parentPath": parent_path, "folderName": folder_name},
                   emit_defaults=True, base_dir=base_dir)
    if isinstance(res, dict) and isinstance(res.get("result"), dict):
        _ensure_ok(res["result"], f"CreateFolder {parent_path}/{folder_name}")
    return (res or {}).get("folderCreated") if isinstance(res, dict) else None


def ensure_folder(config, parent_path, folder_name, base_dir=None):
    """确保目录存在: 先查,不存在再建。返回完整路径。"""
    parent_path = (parent_path or "/").rstrip("/") or "/"
    full = (parent_path + "/" + folder_name).replace("//", "/")
    existing = find_file_by_path(config, parent_path, folder_name, base_dir=base_dir)
    if existing and is_dir(existing):
        return existing.get("fullPathName") or full
    try:
        created = create_folder(config, parent_path, folder_name, base_dir=base_dir)
        if created:
            return created.get("fullPathName") or full
    except Exception:
        pass  # 并发/已存在等竞态,忽略后按路径返回
    return full


def create_file(config, parent_path, file_name, base_dir=None):
    """新建空文件,返回 CreateFileResult{fileHandle}(要写内容见 write_file)。"""
    return _grpcurl(config, "CreateFile", {"parentPath": parent_path, "fileName": file_name},
                    emit_defaults=True, base_dir=base_dir)


# 单次 WriteToFile 的字节上限(分片写,避免超大 base64 撑爆命令行/内存)
WRITE_CHUNK = 4 * 1024 * 1024


def staging_dir(config):
    """写入「受保护路径」时的中转目录(可删)。默认 /Temp/.media_auto_stage。"""
    cd2 = config.get("clouddrive2", {}) or {}
    org = config.get("organize", {}) or {}
    return (cd2.get("staging_dir") or org.get("staging_dir") or "/Temp/.media_auto_stage").rstrip("/")


def _write_blob(config, path, data, base_dir=None, chunk_size=WRITE_CHUNK):
    """真正把二进制内容落到 path。**要求该路径所在区域允许删除**(覆盖前会先删)。

    链路(实测可用):
      CreateFile{parentPath,fileName} -> fileHandle
      WriteToFile{fileHandle,startPos,length,buffer,closeFile} -> bytesWritten
    """
    parent = os.path.dirname(path.rstrip("/")) or "/"
    name = os.path.basename(path.rstrip("/"))

    # 已存在 -> 先删(走回收站),保证覆盖语义且不残留旧内容
    try:
        if find_file_by_path(config, parent, name, base_dir=base_dir):
            delete_files(config, [path], base_dir=base_dir)
    except Exception:  # noqa: BLE001
        pass

    res = create_file(config, parent, name, base_dir=base_dir)
    handle = int((res or {}).get("fileHandle") or 0)
    if not handle:
        raise RuntimeError(f"CreateFile 未返回 fileHandle: {res}")

    written, pos, total = 0, 0, len(data)
    try:
        while pos < total:
            chunk = data[pos:pos + chunk_size]
            out = _grpcurl(config, "WriteToFile", {
                "fileHandle": handle,
                "startPos": pos,
                "length": len(chunk),
                "buffer": base64.b64encode(chunk).decode("ascii"),
                "closeFile": pos + len(chunk) >= total,
            }, emit_defaults=True, base_dir=base_dir)
            n = int((out or {}).get("bytesWritten") or 0)
            if not n:
                n = len(chunk)
            written += n
            pos += len(chunk)
    except Exception:
        # 失败也要关掉句柄,避免 CD2 侧句柄泄漏
        try:
            _grpcurl(config, "CloseFile", {"fileHandle": handle}, emit_defaults=True, base_dir=base_dir)
        except Exception:
            pass
        raise
    return written


def write_file(config, path, content, base_dir=None, chunk_size=WRITE_CHUNK):
    """把 content(str/bytes)写入 CD2 上的文件,返回写入字节数。

    注意:
      - RPC 名是 **WriteToFile**,不是 WriteFile(后者不存在,会报 Unimplemented/找不到方法)。
      - `buffer` 在 proto 里是 `bytes`;grpcurl 用 JSON 传,必须 **base64** 编码。
      - 覆盖已存在的文件需要先删 —— 但 **/Cloud 下禁止删除**。所以目标落在受保护路径时,
        走「写入中转目录 → MoveFile(Overwrite) 搬进去」,全程不删任何东西。
    """
    if isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = bytes(content)

    target_parent = os.path.dirname(path.rstrip("/")) or "/"
    name = os.path.basename(path.rstrip("/"))

    if any(is_under(path, r) for r in no_delete_roots(config)):
        stage = staging_dir(config)
        stage_parent = os.path.dirname(stage) or "/"
        try:
            ensure_folder(config, stage_parent, os.path.basename(stage), base_dir=base_dir)
        except Exception:  # noqa: BLE001
            pass
        stage_path = stage + "/" + name
        _write_blob(config, stage_path, data, base_dir=base_dir, chunk_size=chunk_size)
        # 同区搬入 + Overwrite: 既覆盖旧文件,又不需要删任何东西
        move_file(config, stage_path, target_parent, conflict="Overwrite", base_dir=base_dir)
        return len(data)

    return _write_blob(config, path, data, base_dir=base_dir, chunk_size=chunk_size)


# ---------------------------------------------------------------------------
# 读文件内容 / 拿下载链接(网络通道)
# ---------------------------------------------------------------------------
# proto 里只有 WriteToFile(写), **没有 ReadFile/GetFileContent(读)** —— GetSubFiles 只回
# 目录项与元数据, 拿不到字节。要读回一个已存在文件的全部内容只有两条路:
#   1) WebDAV(受 webdav.account_root 范围限制: 账号只开 /Temp 就读不到 /Cloud)
#   2) GetDownloadUrlPath → HTTP GET(**只要 gRPC token**, 与账号范围都无关)
# 第 2 条是唯一不依赖部署环境的通道, 2026-09-30 为「更新 NFO 读不到现有内容」加上;
# 本地挂载(clouddrive2.local_root)已于同日全面下线, 不再作为读文件方式。
READ_MAX_BYTES = 4 * 1024 * 1024          # 文本类文件(NFO)够用, 也防误下一个大文件


def _download_urls(config, path, preview=True, base_dir=None, timeout=None):
    """GetDownloadUrlPath → [(url, headers), ...]。

    返回的 downloadUrlPath 是模板: /static/{SCHEME}/{HOST}/{PREVIEW}/path?token=…
    (见 proto 注释), 需按真实站点替换占位符 —— gRPC 候选地址有多个(外网/内网),
    这里逐个给出来, 由调用方按序试。
    返回 (列表, 失败原因);列表为空时原因非空。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    res = _grpcurl(config, "GetDownloadUrlPath",
                   {"path": path, "preview": bool(preview), "lazy_read": False,
                    "get_direct_url": False}, timeout=timeout, base_dir=base_dir)
    if not isinstance(res, dict):
        return [], "CD2 没有返回下载链接"
    hdrs = {str(k): str(v) for k, v in (res.get("additionalHeaders") or {}).items()}
    direct = str(res.get("directUrl") or "").strip()
    if direct:
        return [(direct, hdrs)], ""
    tpl = str(res.get("downloadUrlPath") or "").strip()
    if not tpl:
        return [], "CD2 返回的下载链接为空"
    if tpl.startswith("http://") or tpl.startswith("https://"):
        return [(tpl, hdrs)], ""
    out = []
    for target, use_tls, _insecure, _authority in _target_candidates(cd2):
        scheme = "https" if use_tls else "http"
        url = (tpl.replace("{SCHEME}", scheme)
                  .replace("{HOST}", target)
                  .replace("{PREVIEW}", "true" if preview else "false"))
        if not url.startswith("http"):
            url = f"{scheme}://{target}/" + url.lstrip("/")
        out.append((url, hdrs))
    return out, ("" if out else "没有可用的 CD2 站点地址(clouddrive2.hosts)")


def download_urls(config, path, preview=True, base_dir=None, timeout=None):
    """公开版 _download_urls: 供 lib.mediainfo 的「CD2 下载链接」探测通道用。

    返回 ([(url, headers), ...], 失败原因)。路径不存在/没配 hosts 时列表为空、原因非空。
    """
    return _download_urls(config, path, preview=preview, base_dir=base_dir,
                          timeout=timeout)


def _http_get(url, headers=None, timeout=30, verify_tls=True):
    """GET 一小段内容。绕开系统代理(本地/自建地址常被代理挡), insecure 时跳过证书校验。"""
    import ssl
    import urllib.request
    handlers = [urllib.request.ProxyHandler({})]      # 不吃 http_proxy, 与 gRPC 直连一致
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=headers or {})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(READ_MAX_BYTES + 1)


def read_file_text(config, path, timeout=None, max_bytes=READ_MAX_BYTES, base_dir=None):
    """走 CD2 下载链接读回文本文件(**只读**)。

    返回 (text|None, 失败原因)。text 为 None 时原因可用于日志/错误文案。
    只凭 gRPC token, 也不受 WebDAV account_root 范围限制。
    """
    cd2 = config.get("clouddrive2", {}) or {}
    if not timeout:
        timeout = cd2.get("timeout") or 30
    urls, why = _download_urls(config, path, base_dir=base_dir, timeout=timeout)
    if not urls:
        return None, why or "拿不到下载链接"
    errs = []
    for url, hdrs in urls:
        try:
            raw = _http_get(url, hdrs, timeout=timeout,
                            verify_tls=not cd2.get("insecure"))
            if len(raw) > max_bytes:
                return None, f"文件超过 {max_bytes} 字节上限"
            return raw.decode("utf-8", errors="replace"), ""
        except Exception as e:  # noqa: BLE001
            errs.append(f"{url.split('/')[2]}: {str(e).splitlines()[0][:70]}")
    return None, " / ".join(errs) or "下载失败"


# ---------------------------------------------------------------------------
# 状态判定
# ---------------------------------------------------------------------------
# 官方枚举(protojson 输出枚举名):
#   OFFLINE_INIT / OFFLINE_DOWNLOADING / OFFLINE_FINISHED / OFFLINE_ERROR / OFFLINE_UNKNOWN
# 同时兼容老 proto 的 Pending/Downloading/Finished/Error 写法,以及枚举数值。
def _norm_status(status):
    s = str(status or "").strip().upper()
    if s.startswith("OFFLINE_"):
        s = s[len("OFFLINE_"):]
    return s


def is_finished(status):
    return _norm_status(status) in ("FINISHED", "2")


def is_error(status):
    return _norm_status(status) in ("ERROR", "3")


def is_downloading(status):
    return _norm_status(status) in ("DOWNLOADING", "1")


def is_pending(status):
    return _norm_status(status) in ("INIT", "PENDING", "0") or _norm_status(status) in ("UNKNOWN", "4")


def status_text(status):
    """给界面用的中文状态文案。"""
    s = _norm_status(status)
    return {
        "FINISHED": "已完成", "DOWNLOADING": "下载中", "ERROR": "出错",
        "INIT": "等待中", "PENDING": "等待中", "UNKNOWN": "未知",
    }.get(s, str(status or "未知"))


def main():
    import argparse
    ap = argparse.ArgumentParser(description="CD2 gRPC 客户端测试")
    ap.add_argument("action",
                    choices=["accounts", "offline", "offline-by-path", "quota",
                             "subfiles", "find"],
                    help="accounts=发现云账号 offline=账户级离线任务(分页) "
                         "offline-by-path=按目录列离线任务(可能很慢) quota=配额 "
                         "subfiles=列目录 find=按 infoHash/标题查任务")
    ap.add_argument("--path", default="/")
    ap.add_argument("--pages", type=int, default=None, help="分页最多翻几页")
    ap.add_argument("--infohash", default=None)
    ap.add_argument("--name", default=None, help="按名称子串查找")
    args = ap.parse_args()

    root = _PROJECT_ROOT
    from lib.config import load_config as _lc  # noqa: PLC0415
    cfg = _lc()   # 只读数据库
    base = root

    if args.action == "accounts":
        out = list_cloud_accounts(cfg, base_dir=base)
    elif args.action == "offline":
        out = list_offline_all(cfg, max_pages=args.pages, base_dir=base)
    elif args.action == "offline-by-path":
        out = list_offline_by_path(cfg, args.path, base_dir=base)
    elif args.action == "quota":
        out = get_offline_quota(cfg, base_dir=base)
    elif args.action == "find":
        out = find_offline(cfg, info_hash=args.infohash, name_contains=args.name,
                           max_pages=args.pages, base_dir=base)
    else:
        out = get_subfiles(cfg, args.path, base_dir=base)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
