"""媒体信息探测 + tinyMediaManager 风格的标签映射。

用途: 生成 NFO 里的 `<fileinfo><streamdetails>` 与文件名里的质量标记
(如 `2160p h265 EAC3`)。TMM 自己也是用 MediaInfo 取这些值,所以优先走
`mediainfo --Output=JSON`(本机已装),取值与 TMM 最接近。

两条探测通道(probe() 自动依次尝试,都不行就退回"从文件名推断"):
  1) CD2 自带 WebDAV(默认入口 /dav): 先用 ffprobe 直接读 http(s)(Range 只取需要的片段),
     ffprobe 没装/失败再让 mediainfo CLI 直读同一 URL 兜底(它也走 libcurl, 认 URL 内嵌 Basic 凭据)。
     配置见 config.webdav; 断在哪一环会写进日志。
  2) CD2 下载链接(GetDownloadUrlPath → ffprobe 读): 只要 gRPC token, 与 WebDAV 账号范围
     (account_root)无关 —— 账号只开 /Temp 时, 库内 /Cloud 文件也探得到。
  ⚠️ 2026-09-30 起**不再支持本地挂载探测**(clouddrive2.local_root 已全面下线):
     探测与读 NFO 都只走网络通道, 部署无需把 /Cloud 挂到服务同机。

自测: python3 lib/mediainfo.py <本地视频文件>
"""
import base64
import json
import os
import re
import shutil
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit

# 文件名正则的边界: **不要用 \b** —— Python 的 \b 按 \w 判定, 而 \w 含 CJK,
# '中字2160p' / '中字WEB-DL' 里中文与 ASCII 之间不算边界, `\b2160p\b` 会匹配不到,
# 质量/片源/版本在中文标题旁恒猜不出(2026-09-26 审查)。
# 统一改成"前后不能是 ASCII 字母/数字/下划线", 把中文当边界。
_LB = r"(?<![0-9A-Za-z_])"
_RB = r"(?![0-9A-Za-z_])"

# MediaInfo 的 Format -> (NFO 里的 <codec>, 文件名用的标记)
# ---------------------------------------------------------------------------
# 覆盖面尽量全: MediaInfo 的 Format 字符串五花八门(而且同一编码有多种写法),
# 表里查不到时 **不丢弃** —— normalize() 会退回「原样小写」, 避免漏判。
VIDEO_CODECS = {
    # --- 现代主流 ---
    "hevc": ("hevc", "h265"),
    "avc": ("h264", "h264"),
    "av1": ("av1", "AV1"),
    "vp9": ("vp9", "VP9"),
    "vp8": ("vp8", "VP8"),
    "vvc": ("vvc", "H266"),
    "h.266": ("vvc", "H266"),
    "evc": ("evc", "EVC"),
    # --- 上一代 ---
    "vc-1": ("vc1", "VC-1"),
    "vc1": ("vc1", "VC-1"),
    "wmv3": ("wmv3", "WMV3"),
    "wmv2": ("wmv2", "WMV2"),
    "wmv1": ("wmv1", "WMV1"),
    "ms-mpeg4": ("msmpeg4", "MSMPEG4"),
    "ms-mpeg4 v3": ("msmpeg4", "DivX3"),
    # --- MPEG 家族 ---
    "mpeg-4 visual": ("mpeg4", "XviD"),
    "mpeg-4": ("mpeg4", "MPEG-4"),
    "mpeg-2 video": ("mpeg2video", "MPEG-2"),
    "mpeg video": ("mpeg2video", "MPEG-2"),
    "mpeg-1 video": ("mpeg1video", "MPEG-1"),
    # --- 编辑/中间格式 ---
    "prores": ("prores", "ProRes"),
    "dnxhd": ("dnxhd", "DNxHD"),
    "vc-3": ("vc3", "VC-3"),
    "mjpeg": ("mjpeg", "MJPEG"),
    "jpeg 2000": ("jpeg2000", "JPEG2000"),
    # --- 其他 ---
    "theora": ("theora", "Theora"),
    "realvideo 4": ("rv40", "RV40"),
    "realvideo 3": ("rv30", "RV30"),
    "flash video": ("flv1", "FLV"),
    "huffyuv": ("huffyuv", "HuffYUV"),
    "ffv1": ("ffv1", "FFV1"),
    "cinepak": ("cinepak", "Cinepak"),
    "indeo": ("indeo", "Indeo"),
    "h.263": ("h263", "H263"),
    "h.261": ("h261", "H261"),
    "mvc": ("mvc", "MVC"),
}
AUDIO_CODECS = {
    # --- Dolby ---
    "e-ac-3": ("eac3", "EAC3"),
    "eac3": ("eac3", "EAC3"),
    "ac-3": ("ac3", "AC3"),
    "ac3": ("ac3", "AC3"),
    "ac-4": ("ac4", "AC4"),
    "ac4": ("ac4", "AC4"),
    # --- DTS ---
    "dts xll x": ("dtsx", "DTS:X"),
    "dts xll": ("dts", "DTS-HD MA"),
    "dts-hd master audio": ("dts", "DTS-HD MA"),
    "dts-hd": ("dts", "DTS-HD"),
    "dts": ("dts", "DTS"),
    # --- 无损 ---
    "truehd": ("truehd", "TrueHD"),
    "mlp fba": ("truehd", "TrueHD"),
    "flac": ("flac", "FLAC"),
    "alac": ("alac", "ALAC"),
    "monkey's audio": ("ape", "APE"),
    "wavpack": ("wavpack", "WavPack"),
    "tak": ("tak", "TAK"),
    # --- 有损 ---
    "aac": ("aac", "AAC"),
    "opus": ("opus", "Opus"),
    "vorbis": ("vorbis", "Vorbis"),
    "mpeg audio": ("mp3", "MP3"),
    "musepack": ("mpc", "MPC"),
    "speex": ("speex", "Speex"),
    "wma": ("wma", "WMA"),
    "wma pro": ("wmapro", "WMA Pro"),
    "cook": ("cook", "Cook"),
    "real audio": ("real", "RA"),
    "amr": ("amr", "AMR"),
    "ac-3 lc": ("ac3", "AC3"),
    # --- PCM 家族 ---
    "pcm": ("pcm_s16le", "PCM"),
    "lpcm": ("pcm_s16le", "PCM"),
    "adpcm": ("adpcm", "ADPCM"),
    "dvd-audio": ("pcm_s16le", "PCM"),
    "mlp": ("truehd", "MLP"),
}
# 字幕格式(不进文件名,不影响 <codec>,仅做归类;查不到也不丢弃)
SUBTITLE_CODECS = {
    "subrip": "subrip", "subrip/text": "subrip", "s_text/utf8": "subrip",
    "ass": "ass", "ssa": "ssa",
    "pgs": "pgs", "hdmv pgs": "pgs",
    "vobsub": "vobsub", "dvd subtitles": "vobsub",
    "utf-8": "subrip", "text": "subrip",
    "webvtt": "webvtt", "eia-608": "eia608", "eia-708": "eia708",
    "microdvd": "microdvd",
}

# 语言: 2 字母 / 3 字母 -> ISO 639-2 三字母(NFO 里 TMM 用的是 3 字母,如 zho/eng)
LANG3 = {
    "zh": "zho", "chi": "zho", "zho": "zho", "cmn": "zho", "yue": "chi",
    "en": "eng", "eng": "eng", "ja": "jpn", "jpn": "jpn", "ko": "kor", "kor": "kor",
    "fr": "fra", "fre": "fra", "fra": "fra", "de": "deu", "ger": "deu", "deu": "deu",
    "es": "spa", "spa": "spa", "it": "ita", "ita": "ita", "ru": "rus", "rus": "rus",
    "pt": "por", "por": "por", "th": "tha", "tha": "tha", "vi": "vie", "vie": "vie",
    "id": "ind", "ind": "ind", "ms": "msa", "msa": "msa", "ar": "ara", "ara": "ara",
    "hi": "hin", "hin": "hin", "nl": "nld", "dut": "nld", "pl": "pol", "pol": "pol",
    "sv": "swe", "swe": "swe", "da": "dan", "dan": "dan", "no": "nor", "nor": "nor",
    "fi": "fin", "fin": "fin", "tr": "tur", "tur": "tur", "he": "heb", "heb": "heb",
    "uk": "ukr", "ukr": "ukr", "cs": "ces", "cze": "ces", "ces": "ces",
    "hu": "hun", "hun": "hun", "el": "ell", "gre": "ell", "ell": "ell",
    "fa": "fas", "per": "fas", "fas": "fas", "bn": "ben", "ben": "ben",
    "ta": "tam", "tam": "tam", "te": "tel", "tel": "tel", "ml": "mal", "mal": "mal",
    "fil": "fil", "tl": "fil", "my": "mya", "bur": "mya", "mya": "mya",
    "km": "khm", "khm": "khm", "lo": "lao", "lao": "lao", "si": "sin", "sin": "sin",
}


def lang3(value):
    """把 MediaInfo 的语言标识规整成 3 字母。识别不了就原样返回。"""
    if not value:
        return ""
    v = str(value).strip().lower()
    v = v.split("-")[0].split("_")[0]
    return LANG3.get(v, v if len(v) == 3 else v)


def _find_cli():
    for name in ("mediainfo", "MediaInfo"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/opt/homebrew/bin/mediainfo", "/usr/local/bin/mediainfo", "/usr/bin/mediainfo"):
        if os.path.exists(p):
            return p
    return None


def _note(err_out, msg):
    """把失败原因累积到 err_out(list),调用方据此在日志里说明断在哪一环。"""
    if err_out is not None:
        err_out.append(str(msg))


def probe_file(path, timeout=180, err_out=None):
    """直接探测一个本地文件路径(MediaInfo CLI)。失败返回 None。"""
    cli = _find_cli()
    if not cli:
        _note(err_out, "mediainfo CLI 未安装/未找到")
        return None
    if not path or not os.path.exists(path):
        _note(err_out, f"本地路径不存在: {path}")
        return None
    try:
        proc = subprocess.run([cli, "--Output=JSON", path], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _note(err_out, f"mediainfo 超时({timeout}s)")
        return None
    except OSError as e:
        _note(err_out, f"mediainfo 无法执行: {e}")
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        _note(err_out, f"mediainfo 退出码 {proc.returncode}: "
                       f"{(proc.stderr or '').strip()[:200]}")
        return None
    try:
        return normalize(json.loads(proc.stdout))
    except Exception as e:  # noqa: BLE001
        _note(err_out, f"mediainfo 输出解析失败: {e}")
        return None


# ---------------------------------------------------------------------------
# WebDAV 通道(CD2 自带)—— 探测/读文件的主通道
# ---------------------------------------------------------------------------
# 用 CD2 的 WebDAV(默认 /dav)直接读。MediaInfo CLI 不吃 URL,
# 但 ffprobe 支持 http/https —— 它会自己用 Range 请求只取需要的片段
# (mp4 的 moov、mkv 的 header),不会把 17GB 全下下来,所以很快。
def webdav_conf(config):
    return ((config or {}).get("webdav") or {})


def webdav_url(cd2_path, config=None):
    """CD2 路径 -> WebDAV URL。

    config.webdav:
      base         WebDAV 入口,如 https://nas.example.com:5002/dav
      account_root 该账号的根对应 CD2 上的哪个路径(如 /Temp)。
                   只有落在它下面的 CD2 路径才能映射出 URL;范围外返回 None。
      user/password  Basic 认证(也支持直接给 authorization 头)
    """
    wd = webdav_conf(config)
    base = str(wd.get("base") or "").strip().rstrip("/")
    if not base or not cd2_path:
        return None
    root = str(wd.get("account_root") or "/").strip().rstrip("/")
    p = "/" + str(cd2_path).lstrip("/")
    if root and root != "/":
        if p == root:
            rel = ""
        elif p.startswith(root + "/"):
            rel = p[len(root):]
        else:
            return None          # 不在该 WebDAV 账号范围内
    else:
        rel = p
    return base + quote(rel or "/", safe="/")


def _auth_header(config):
    """WebDAV 认证头: 优先用现成的 authorization,否则由 user/password 组 Basic。"""
    wd = webdav_conf(config)
    if wd.get("authorization"):
        return str(wd["authorization"])
    user, pwd = wd.get("user"), wd.get("password")
    if user and pwd is not None:
        raw = f"{user}:{pwd}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")
    return ""


def webdav_get_text(cd2_path, config=None, timeout=30):
    """通过 CD2 WebDAV(GET)读取文本文件内容(如 NFO / .nfo)。

    用于「账号范围覆盖不到之外的场景仍能读到现有文件内容」—— 例如手动更新 NFO 时要保留
    原 NFO 里的 <fileinfo> 段(媒体流信息无法从 TMDB 元数据推导,只能从现有文件抽)。
    返回解码后的文本; 任意环节失败(未配置 / 路径不在 WebDAV 账号范围 / 网络错误)返回 None。
    """
    url = webdav_url(cd2_path, config)
    if not url:
        return None
    auth = _auth_header(config)
    headers = {}
    if auth:
        headers["Authorization"] = auth
    ua = (webdav_conf(config).get("user_agent") or "").strip()
    if ua:
        headers["User-Agent"] = ua
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)                      # 本地代理会挡掉内网/自建地址
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _ffprobe_cli():
    for name in ("ffprobe",):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe"):
        if os.path.exists(p):
            return p
    return None


def _ratio(value):
    """把 '2.40:1' / '16:9' / '2.40' 统一成 TMM 风格的小数(2 位有效)。"""
    if not value:
        return ""
    s = str(value).strip()
    try:
        if ":" in s:
            a, b = s.split(":", 1)
            n = float(a) / float(b)
        else:
            n = float(s.split()[0])
        return f"{n:.2f}"          # 与 TMM/MediaInfo 的写法一致(如 2.40 / 1.78)
    except Exception:  # noqa: BLE001
        return s


# 常见"标称"宽高比。MediaInfo(=TMM 用的那个)报的是标称值而不是像素比:
# 例如 1920x808 实测 MediaInfo 报 2.40(像素比 2.376)、指环王3 1920x798 报 2.40(像素比 2.406)。
# ffprobe 在没有 DAR 标记时会按像素算,于是出现 2.38 / 2.41 这类偏差。
# 这里把像素比吸附到最近的标称值(容差 0.02),让 WebDAV(ffprobe)通道与本地(MediaInfo)通道产出一致。
NOMINAL_ASPECTS = [1.33, 1.37, 1.50, 1.66, 1.78, 1.85, 2.00, 2.20, 2.35, 2.40, 2.76]


def _snap_aspect(value, tol=0.03):
    """把像素宽高比吸附到最近的标称值(如 2.41 -> 2.40)。

    容差 0.03 是根据实测定的: 1920x808 的像素比是 2.376,而 MediaInfo/TMM 报 2.40
    (差 0.024);再小就吸不过去。2.35 与 2.40 的分界在 2.375,所以 2.376 归到 2.40,
    这与 MediaInfo 对 scope 片(2.40:1)的标注一致。
    解析失败或离所有标称值都远时原样返回(不硬套)。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        n = float(s)
    except ValueError:
        return s
    best, dist = None, None
    for cand in NOMINAL_ASPECTS:
        d = abs(n - cand)
        if dist is None or d < dist:
            best, dist = cand, d
    if best is not None and dist is not None and dist <= tol:
        return f"{best:.2f}"
    return s


def _fps(value):
    s = str(value or "").strip()
    if "/" in s:
        try:
            a, b = s.split("/", 1)
            return round(float(a) / float(b), 3)
        except Exception:  # noqa: BLE001
            return 0.0
    return _f(s)


def normalize_ff(data):
    """把 ffprobe 的 JSON 规整成与 normalize() 相同的结构。

    ffprobe 的 codec_name 与 MediaInfo 的 Format 写法不同(h264 vs avc、ac3 vs ac-3),
    所以先做一次别名映射,再去查同一张表 —— 保证两条通道产出的 <codec> 完全一致。
    """
    fmt = (data or {}).get("format", {}) or {}
    streams = (data or {}).get("streams", []) or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    audios_raw = [s for s in streams if s.get("codec_type") == "audio"]
    subs_raw = [s for s in streams if s.get("codec_type") == "subtitle"]

    def _tags(s):
        return s.get("tags") or {}

    vraw = (v.get("codec_name") or "").strip().lower()
    vfmt = _FF_VIDEO_ALIAS.get(vraw, vraw)
    vcodec, vlabel = VIDEO_CODECS.get(vfmt, (vraw or "", (vraw or "").upper()))
    res_label, res_num = resolution_label(v.get("width"), v.get("height"))

    # 宽高比: 优先用 DAR 标记;没有就按像素算,再吸附到标称值
    _dar = _ratio(v.get("display_aspect_ratio"))
    if not _dar and v.get("width") and v.get("height"):
        _dar = _ratio(f"{v.get('width')}:{v.get('height')}")

    video = {
        "codec": vcodec,
        "codec_label": vlabel,
        "width": _i(v.get("width")),
        "height": _i(v.get("height")),
        "aspect": _snap_aspect(_dar),
        "resolution": res_num,
        "resolution_label": res_label,
        "bitdepth": _i(v.get("bits_per_raw_sample"))
                    or ({"yuv420p10le": 10, "yuv420p12le": 12, "yuv444p10le": 10}.get(v.get("pix_fmt") or "", 0))
                    or _i(v.get("bits_per_sample")),
        "fps": _fps(v.get("r_frame_rate") or v.get("avg_frame_rate")),
        "profile": v.get("profile") or "",
    }

    audios = []
    for a in audios_raw:
        araw = (a.get("codec_name") or "").strip().lower()
        afmt = _FF_AUDIO_ALIAS.get(araw, "pcm" if araw.startswith("pcm") else araw)
        acodec, alabel = AUDIO_CODECS.get(afmt, (araw or "", (araw or "").upper()))
        audios.append({
            "codec": acodec,
            "codec_label": alabel,
            "channels": _i(a.get("channels")),
            "layout": a.get("channel_layout") or "",
            "language": lang3(_tags(a).get("language")),
            "title": _tags(a).get("title") or "",
            "bitrate": _i(a.get("bit_rate")),
        })

    subtitles = []
    for s in subs_raw:
        subtitles.append({
            "codec": (s.get("codec_name") or "").strip().lower(),
            "language": lang3(_tags(s).get("language")),
            "title": _tags(s).get("title") or "",
            "forced": bool(((s.get("disposition") or {}).get("forced"))),
        })

    duration = _f(fmt.get("duration"))
    return {
        "duration_secs": int(round(duration)),
        "duration": duration,
        "size": _i(fmt.get("size")),
        "bitrate": _i(fmt.get("bit_rate")),
        "container": (fmt.get("format_name") or "").split(",")[0],
        "video": video,
        "audios": audios,
        "subtitles": subtitles,
        "audio_primary": pick_audio(audios),
    }


# ffprobe codec_name -> MediaInfo Format(便于复用同一张编码表)
_FF_VIDEO_ALIAS = {
    "h264": "avc", "h265": "hevc", "hevc": "hevc",
    "av1": "av1", "vp9": "vp9", "vp8": "vp8", "vvc": "vvc", "h266": "vvc",
    "mpeg4": "mpeg-4 visual", "msmpeg4v3": "ms-mpeg4 v3", "msmpeg4v2": "ms-mpeg4",
    "mpeg2video": "mpeg-2 video", "mpeg1video": "mpeg-1 video",
    "vc1": "vc-1", "wmv3": "wmv3", "wmv2": "wmv2", "wmv1": "wmv1",
    "prores": "prores", "dnxhd": "dnxhd", "vc3": "vc-3",
    "mjpeg": "mjpeg", "jpeg2000": "jpeg 2000", "theora": "theora",
    "rv40": "realvideo 4", "rv30": "realvideo 3", "flv1": "flash video",
    "h263": "h.263", "h261": "h.261", "mvc": "mvc",
}
_FF_AUDIO_ALIAS = {
    "ac3": "ac-3", "eac3": "e-ac-3", "ac4": "ac-4",
    "dts": "dts", "truehd": "truehd", "mlp": "mlp",
    "aac": "aac", "flac": "flac", "alac": "alac",
    "opus": "opus", "vorbis": "vorbis",
    "mp3": "mpeg audio", "mp2": "mpeg audio", "mp1": "mpeg audio",
    "wmav2": "wma", "wmapro": "wma pro", "cook": "cook", "ra_144": "real audio",
    "ape": "monkey's audio", "wavpack": "wavpack", "musepack7": "musepack",
    "speex": "speex", "amr_nb": "amr", "adpcm_ms": "adpcm",
}


def probe_url(url, config=None, timeout=180, err_out=None, headers=None,
              webdav_auth=True, verify_tls=True):
    """用 ffprobe 探测一个 http(s) 资源。失败返回 None。

    webdav_auth=True → 附上 WebDAV 的 Authorization 与 user-agent(探 WebDAV URL 用);
                       下载链接是 token 自鉴权, 传 False 免得把 WebDAV 凭据带给别的站点。
    headers          → 额外请求头(CD2 下载链接返回的 additionalHeaders), 与上面合成一次 -headers
                       (ffmpeg 的 -headers 只吃一个值, 分开传会互相覆盖)。
    verify_tls=False → 跳过证书校验(clouddrive2.insecure, 自签证书)。
    """
    cli = _ffprobe_cli()
    if not cli:
        _note(err_out, "ffprobe 未安装/未找到")
        return None
    if not url:
        _note(err_out, "探测 URL 为空")
        return None
    cmd = [cli, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams"]
    hdrs = []
    if webdav_auth:
        auth = _auth_header(config)
        if auth:
            hdrs.append(f"Authorization: {auth}")
        ua = (webdav_conf(config).get("user_agent") or "").strip()
        if ua:
            hdrs.append(f"User-Agent: {ua}")
    for h in (headers or []):
        h = str(h).strip().strip("\r\n")
        if h and h not in hdrs:
            hdrs.append(h)
    if hdrs:
        cmd += ["-headers", "".join(f"{h}\r\n" for h in hdrs)]
    if not verify_tls:
        cmd += ["-tls_verify", "0"]          # 自签证书(clouddrive2.insecure)
    cmd.append(url)
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)                      # 本地代理会挡掉内网/自建地址
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        _note(err_out, f"ffprobe 超时({timeout}s): {url}")
        return None
    except OSError as e:
        _note(err_out, f"ffprobe 无法执行: {e}")
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        err = (proc.stderr or "").strip().replace("\n", " ")[:300]
        _note(err_out, f"ffprobe 退出码 {proc.returncode}: {err} ← {url}")
        return None
    try:
        info = normalize_ff(json.loads(proc.stdout))
    except Exception as e:  # noqa: BLE001
        _note(err_out, f"ffprobe 输出解析失败: {e}")
        return None
    info["url"] = url
    return info


def _with_basic_auth(url, config):
    """把 webdav.user/password 内嵌进 URL(mediainfo 的 libcurl 认 https://user:pass@host)。

    已内嵌 / 没配明文密码(只给了 authorization 头)→ 原样返回, 由调用方决定要不要试。
    """
    wd = webdav_conf(config)
    user, pwd = wd.get("user"), wd.get("password")
    if not user or pwd is None:
        return url
    p = urlsplit(url)
    if p.username:
        return url
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    netloc = f"{quote(str(user), safe='')}:{quote(str(pwd), safe='')}@{host}"
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def probe_url_mediainfo(url, config=None, timeout=180, err_out=None, embed_auth=True):
    """ffprobe 缺失/失败时的兜底: 让 mediainfo CLI 直接读 URL(默认 WebDAV)。

    产出经 normalize() 与 ffprobe 通道同构, 所以调用方拿去写 <fileinfo> 无差别 ——
    这条是为了「镜像里只装了 mediainfo、没有 ffmpeg」的部署不至于整个丢掉 fileinfo。
    embed_auth=False(下载链接)时不把 WebDAV 账密内嵌进 URL: 那是别站的 token 鉴权。
    """
    cli = _find_cli()
    if not cli:
        _note(err_out, "mediainfo CLI 未安装/未找到")
        return None
    if not url:
        _note(err_out, "探测 URL 为空")
        return None
    target = _with_basic_auth(url, config) if embed_auth else url
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(k, None)
    try:
        proc = subprocess.run([cli, "--Output=JSON", target], capture_output=True,
                              text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        _note(err_out, f"mediainfo 读 URL 超时({timeout}s): {url}")
        return None
    except OSError as e:
        _note(err_out, f"mediainfo 无法执行: {e}")
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        _note(err_out, f"mediainfo 读 URL 失败(退出码 {proc.returncode}): {err} ← {url}")
        return None
    try:
        info = normalize(json.loads(proc.stdout))
    except Exception as e:  # noqa: BLE001
        _note(err_out, f"mediainfo 输出解析失败: {e}")
        return None
    # 401/403 时 mediainfo 不报错, 只回 "media":null → 视为没读到
    if not (info.get("duration_secs") or 0) and not (info.get("video") or {}).get("width"):
        _note(err_out, f"mediainfo 未解析出媒体流(多半是 401/403 或不是媒体): {url}")
        return None
    info["url"] = url
    return info


def probe_timeout(config):
    """探测超时(秒): 0 / 负数 / 乱码 → 默认 180, 上限 600。

    老代码会把配置原样传给 subprocess.run —— 负数会让 mediainfo 与 ffprobe 两条
    通道**都**立刻超时, 所有 <fileinfo> 变空, 日志却只写 "ffprobe 超时(-5s)",
    根本看不出是配置填错了。"""
    try:
        t = int(float(((config or {}).get("webdav") or {}).get("timeout") or 180))
    except Exception:  # noqa: BLE001
        t = 180
    if t <= 0:        # 0 / 负数 = 没配或配错 → 用默认, 不是"0 秒超时"
        t = 180
    return min(t, 600)


def _usable(info):
    """探测结果必须带【视频流】(编码 + 宽度)才算有效。

    moov 在尾 / Range 读不全 / 401 时, 两条通道都可能吐出"只有时长、没有流"的
    残缺 info; 拿它写 <fileinfo> 会把缺音轨缺字幕的结果**永久固化**(下次更新还会
    因为"已有 fileinfo"不再补探)。宁可判失败, 让调用方退回文件名推断/留空。"""
    v = (info or {}).get("video") or {}
    return bool((v.get("codec") or "").strip() and (v.get("width") or 0))


def _probe_via_url(url, config, timeout, reasons, headers=None,
                   webdav_auth=True, verify_tls=True):
    """ffprobe → mediainfo CLI 兜底地探测一个 http(s) URL, 成功返回 info(带 url), 否则 None。"""
    info = probe_url(url, config, timeout=timeout, err_out=reasons, headers=headers,
                     webdav_auth=webdav_auth, verify_tls=verify_tls)
    if info and not _usable(info):
        reasons.append("ffprobe: 结果缺视频流(moov 在尾 / Range 读不全?)")
        info = None
    if not info:
        # ffprobe 没装(镜像漏建)或读不动 → 让 mediainfo CLI 直读同一 URL 兜底
        info = probe_url_mediainfo(url, config, timeout=timeout, err_out=reasons,
                                   embed_auth=webdav_auth)
        if info and not _usable(info):
            reasons.append("mediainfo: 结果缺视频流(moov 在尾 → 只读到时长)")
            info = None
    return info


def probe(cd2_path, config=None, base_dir=None, timeout=180, log=None):
    """按 CD2 路径探测媒体信息。两条通道依次尝试(2026-09-30 起 local_root 已下线):

      1) CD2 WebDAV   -> ffprobe over HTTP(Range 只取需要的片段), mediainfo CLI 兜底
      2) CD2 下载链接  -> ffprobe over HTTP(只要 gRPC token, 不受 webdav.account_root 限制)

    都不可用返回 None(调用方退回"从文件名推断质量标记"),并把**失败原因**
    交给 log(若提供)—— 否则 <fileinfo> 为空时看不出到底断在哪一环。
    """
    reasons = []

    # --- 1) CD2 WebDAV(主通道, 要 user/password 或 authorization) ---
    wd = webdav_conf(config)
    if not wd.get("enabled", True):
        reasons.append("WebDAV: webdav.enabled=false")
    elif not (_auth_header(config) or wd.get("allow_anonymous")):
        reasons.append("WebDAV: 未配 user/password(或 authorization),已跳过")
    else:
        url = webdav_url(cd2_path, config)
        if not url:
            reasons.append(f"WebDAV: 路径 {cd2_path} 不在 account_root="
                           f"{wd.get('account_root')!r} 范围内(如账号只开到 /Temp)")
        else:
            info = _probe_via_url(url, config, timeout, reasons)
            if info:
                info["source"] = "webdav"
                return info

    # --- 2) CD2 下载链接(只要有 gRPC token, account_root 管不到它) ---
    try:
        from clients.clouddrive import client as _cd2
        urls, why = _cd2.download_urls(config, cd2_path, base_dir=base_dir,
                                       timeout=timeout)
    except Exception as e:  # noqa: BLE001
        urls, why = [], str(e)[:160]
    if not urls:
        reasons.append(f"下载链接: {why or '拿不到'}")
    else:
        verify_tls = not ((config or {}).get("clouddrive2") or {}).get("insecure")
        per_url = []
        for url, hdrs in urls:
            one = []
            hlist = [f"{k}: {v}" for k, v in (hdrs or {}).items()]
            info = _probe_via_url(url, config, timeout, one, headers=hlist,
                                  webdav_auth=False, verify_tls=verify_tls)
            if info:
                info["source"] = "download"
                return info
            per_url.append(one[-1] if one else "读不到")
        reasons.append("下载链接: " + " / ".join(dict.fromkeys(
            e.split(" ← ")[0][:80] for e in per_url[:3])))

    if log:
        log(f"    ⚠ <fileinfo> 探测失败,写空标签 — " + " | ".join(reasons))
    return None


def _f(v):
    try:
        return float(str(v).split()[0])
    except Exception:  # noqa: BLE001
        return 0.0


def _i(v):
    try:
        return int(float(str(v).split()[0]))
    except Exception:  # noqa: BLE001
        return 0


def resolution_label(width, height):
    """按宽度优先推算分辨率标记(TMM 风格: 2160p/1080p/720p/480p)。

    注意不能用高度直接判: 宽银幕 1920x808 也是 1080p,4K 的 4096x1698 是 2160p。
    """
    w, h = _i(width), _i(height)
    if w >= 3600 or h >= 1900:
        return "2160p", 2160
    if w >= 1700 or h >= 950:
        return "1080p", 1080
    if w >= 1100 or h >= 650:
        return "720p", 720
    if w >= 700 or h >= 400:
        return "480p", 480
    return (f"{w}p", w) if w else ("", 0)


def normalize(mi):
    """把 MediaInfo 的 JSON 规整成统一结构。"""
    tracks = (mi or {}).get("media", {}).get("track", []) or []
    gen = next((t for t in tracks if t.get("@type") == "General"), {})
    v = next((t for t in tracks if t.get("@type") == "Video"), {})
    audios_raw = [t for t in tracks if t.get("@type") == "Audio"]
    subs_raw = [t for t in tracks if t.get("@type") == "Text"]

    vfmt = (v.get("Format") or "").strip().lower()
    vcodec, vlabel = VIDEO_CODECS.get(vfmt, (vfmt or "", (v.get("Format") or "").upper()))
    res_label, res_num = resolution_label(v.get("Width"), v.get("Height"))

    aspect = v.get("DisplayAspectRatio") or ""
    if aspect:
        # 统一走 _ratio + _snap_aspect: 与 ffprobe 通道同口径(TMM 风格 '2.40',
        # 且 2.376 → 2.40),否则同一条片在两条通道下 aspect 一个 '2.4' 一个 '2.40'。
        aspect = _snap_aspect(_ratio(aspect)) or str(aspect)

    duration = _f(gen.get("Duration")) or _f(v.get("Duration"))
    video = {
        "codec": vcodec,
        "codec_label": vlabel,
        "width": _i(v.get("Width")),
        "height": _i(v.get("Height")),
        "aspect": aspect,
        "resolution": res_num,
        "resolution_label": res_label,
        "bitdepth": _i(v.get("BitDepth")),
        "fps": _f(v.get("FrameRate")),
        "profile": v.get("Format_Profile") or "",
    }

    audios = []
    for a in audios_raw:
        afmt = (a.get("Format") or "").strip().lower()
        acodec, alabel = AUDIO_CODECS.get(afmt, (afmt or "", (a.get("Format") or "").upper()))
        audios.append({
            "codec": acodec,
            "codec_label": alabel,
            "channels": _i(a.get("Channels")),
            "layout": a.get("ChannelLayout") or "",
            "language": lang3(a.get("Language")),
            "title": a.get("Title") or "",
            "bitrate": _i(a.get("BitRate")),
        })

    subtitles = []
    for s in subs_raw:
        subtitles.append({
            "codec": (s.get("Format") or "").strip().lower(),
            "language": lang3(s.get("Language")),
            "title": s.get("Title") or "",
            "forced": str(s.get("Forced") or "").lower() == "yes",
        })

    return {
        "duration_secs": int(round(duration)),
        "duration": duration,
        "size": _i(gen.get("FileSize")),
        "bitrate": _i(gen.get("OverallBitRate")),
        "container": gen.get("Format") or "",
        "video": video,
        "audios": audios,
        "subtitles": subtitles,
        "audio_primary": pick_audio(audios),
    }


def pick_audio(audios):
    """挑"代表音轨": 优先无损/高规格(TrueHD/DTS-HD > DTS/EAC3/AC3 > AAC…),声道多者优先。

    用于文件名里的音频标记(如 EAC3)。TMM 取的是第一条音轨,但排一下更符合直觉,
    两条都能用;要严格跟 TMM 一致可在配置里关掉 organize.audio_pick_best。
    """
    if not audios:
        return None
    rank = {"truehd": 0, "dts": 1, "eac3": 2, "ac3": 3, "flac": 1,
            "aac": 4, "opus": 4, "vorbis": 4, "mp3": 5, "pcm_s16le": 1}
    return sorted(audios, key=lambda a: (rank.get(a.get("codec"), 6), -int(a.get("channels") or 0)))[0]


def quality_tag(info, pick_best_audio=True):
    """文件名用的质量标记,如 `2160p h265 EAC3` / `1080p h265 AC3`。"""
    if not info:
        return ""
    parts = []
    v = info.get("video") or {}
    if v.get("resolution_label"):
        parts.append(v["resolution_label"])
    if v.get("codec_label"):
        parts.append(v["codec_label"])
    a = pick_audio(info.get("audios") or []) if pick_best_audio else (info.get("audios") or [None])[0]
    if a and a.get("codec_label"):
        parts.append(a["codec_label"])
    return " ".join(parts)


def _tag_for(table, text):
    for pat, label in table:
        if re.search(pat, text, re.IGNORECASE):
            return label
    return ""


# 从发布名里推质量标记(探测不可用时的兜底)。顺序敏感: 先具体(DDP/EAC3), 再 AC3/DTS 等。
# 边界用 _LB/_RB(见文件头): '标题中字2160p' 这种中文贴着数字的写法 \b 匹配不到。
_NAME_RES = [(rf"{_LB}(?:8k|4320p){_RB}", "4320p"), (rf"{_LB}(?:4k|2160p|uhd){_RB}", "2160p"),
             (rf"{_LB}1080[pi]{_RB}", "1080p"), (rf"{_LB}720[pi]{_RB}", "720p"),
             (rf"{_LB}(?:576[pi]|480[pi]|sd){_RB}", "480p")]
_NAME_VCODEC = [(rf"{_LB}(?:x265|h\.?265|hevc){_RB}", "h265"), (rf"{_LB}(?:x264|h\.?264|avc){_RB}", "h264"),
                (rf"{_LB}av1{_RB}", "av1"), (rf"{_LB}vp9{_RB}", "vp9"),
                (rf"{_LB}(?:mpeg-?2|xvid|divx){_RB}", "mpeg2")]
_NAME_ACODEC = [(rf"{_LB}(?:atmos|dd\+|ddp|eac3|e-ac-3)", "EAC3"),
                (rf"{_LB}truehd{_RB}", "TrueHD"), (rf"{_LB}dts[-.\s]?(?:hd|ma|x){_RB}", "DTSHD"),
                (rf"{_LB}dts{_RB}", "DTS"), (rf"{_LB}ac3{_RB}", "AC3"), (rf"{_LB}(?:dd5\.1|dd){_RB}", "AC3"),
                (rf"{_LB}aac{_RB}", "AAC"), (rf"{_LB}flac{_RB}", "FLAC"), (rf"{_LB}(?:lpcm|pcm){_RB}", "PCM"),
                (rf"{_LB}opus{_RB}", "Opus"), (rf"{_LB}mp3{_RB}", "MP3")]

def quality_from_name(name):
    """只靠文件名推质量标记,如 `Remain.Silent.2019.2160p.WEB-DL.H265.10bit.DDP5.1`
    → `2160p h265 EAC3`(与 MediaInfo 实探结果一致)。

    用途: 两条探测通道都拿不到时兜底,保证文件名仍有质量标记。
    """
    if not name:
        return ""
    text = str(name)
    parts = [_tag_for(_NAME_RES, text),
             _tag_for(_NAME_VCODEC, text),
             _tag_for(_NAME_ACODEC, text)]
    return " ".join([p for p in parts if p])


# 分辨率权重(用于比较两个版本的画质高低)
_RES_WEIGHT = {"4320p": 5, "2160p": 4, "1080p": 3, "720p": 2, "480p": 1}
# 片源权重: 原盘/Remux > BluRay > WEB-DL > HDTV/其他
_SOURCE_WEIGHT = {"REMUX": 4, "BLURAY": 3, "WEBDL": 2, "WEBRIP": 2, "HDTV": 1}


def resolution_rank(text):
    """从文件名/质量标记取分辨率权重。`2160p`→4, `1080p`→3;未知→0。"""
    m = re.search(rf"{_LB}(4320p|2160p|1080[pi]|720[pi]|576[pi]|480[pi]){_RB}",
                  str(text or ""), re.IGNORECASE)
    if not m:
        return 0
    key = m.group(1).lower().replace("i", "p")
    if key in ("576p",):
        return 1
    return _RES_WEIGHT.get(key, 0)


def source_rank(text):
    """从文件名推断片源权重(REMUX 4 / BLURAY 3 / WEBDL 2 / HDTV 1 / 未知 0)。"""
    return _SOURCE_WEIGHT.get(guess_source(text), 0)


def version_info(name, size=0):
    """把一个版本规整成可比较/可展示的字典。"""
    return {
        "name": name,
        "quality": quality_from_name(name),
        "resolution": resolution_rank(name),
        "source": guess_source(name),
        "source_rank": source_rank(name),
        "size": int(size or 0),
    }


def compare_versions(new_name, new_size, old_name, old_size):
    """判断新版本相对库里已有版本是升级还是降级。

    比较顺序(重要):
      1. **分辨率**优先 —— 4K 版永远好于 1080p 版,无歧义。
      2. 分辨率相同时比**体积**(差 >15% 才算实质差异)——
         因为库里的成品文件名只留 `标题 (年份) 质量`(_我们的模板就不写片源_),
         拿它跟新种子的 `BLURAY/REMUX` 标记比片源是**不可靠**的,
         而体积差正好反映码率/音轨规格的差距。
      3. 体积也接近时,才用**片源**做最后的裁决,且要求**两侧都识别得出片源**
         (库内多为 "NONE",此时不参与比较)。

    返回 dict: verdict(upgrade/downgrade/same) / better / why / new / old
    """
    new_i, old_i = version_info(new_name, new_size), version_info(old_name, old_size)
    rn, ro = new_i["resolution"], old_i["resolution"]

    def gb(n):
        return f"{n / 1024 ** 3:.1f}GB"

    if rn and ro and rn != ro:
        higher = rn > ro
        verdict = "upgrade" if higher else "downgrade"
        why = (f"{'画质更高' if higher else '画质更低'}: 新 {new_i['quality'] or '未知'} "
               f"vs 库内 {old_i['quality'] or '未知'}")
    elif new_size and old_size and max(new_size, old_size) > min(new_size, old_size) * 1.15:
        higher = new_size > old_size
        verdict = "upgrade" if higher else "downgrade"
        same_res = f"同为 {new_i['quality'] or old_i['quality'] or '未知规格'}," if rn and rn == ro else ""
        why = (f"{same_res}{'体积更大(码率/音轨规格更高)' if higher else '体积更小'}: "
               f"{gb(new_size)} vs 库内 {gb(old_size)}")
    elif new_i["source_rank"] and old_i["source_rank"] and new_i["source_rank"] != old_i["source_rank"]:
        higher = new_i["source_rank"] > old_i["source_rank"]
        verdict = "upgrade" if higher else "downgrade"
        why = (f"{'片源更好' if higher else '片源更差'}: 新 {new_i['source']} "
               f"vs 库内 {old_i['source']}")
    else:
        verdict, why = "same", (f"与库内版本规格基本一致({gb(new_size)} vs {gb(old_size)})"
                                if new_size and old_size else "与库内版本规格基本一致")

    return {"verdict": verdict, "better": verdict == "upgrade", "why": why,
            "new": new_i, "old": old_i}


def streamdetails_xml(info, indent="  "):
    """生成 NFO 里的 <fileinfo><streamdetails>…</streamdetails></fileinfo>。

    默认 indent="  " —— `<fileinfo>` 落在 2 空格缩进,正是 NFO 顶层元素的层级
    (TMM 实测: fileinfo=2 / streamdetails=4 / video=6 / codec=8)。
    注意: 本函数【已包含 <fileinfo> 外壳】,调用方不要再包一层
    (早期踩坑: build_movie_nfo 又包了一次,产出 `<fileinfo><fileinfo>` 嵌套)。
    """
    if not info:
        return ""
    i1, i2, i3 = indent, indent + "  ", indent + "    "
    L = [f"{i1}<fileinfo>", f"{i2}<streamdetails>"]
    v = info.get("video") or {}
    L.append(f"{i3}<video>")
    if v.get("codec"):
        L.append(f"{i3}  <codec>{v['codec']}</codec>")
    if v.get("aspect"):
        L.append(f"{i3}  <aspect>{v['aspect']}</aspect>")
    if v.get("width"):
        L.append(f"{i3}  <width>{v['width']}</width>")
    if v.get("height"):
        L.append(f"{i3}  <height>{v['height']}</height>")
    if v.get("resolution"):
        L.append(f"{i3}  <resolution>{v['resolution']}</resolution>")
    if info.get("duration_secs"):
        L.append(f"{i3}  <durationinseconds>{info['duration_secs']}</durationinseconds>")
    L.append(f"{i3}</video>")
    for a in info.get("audios") or []:
        L.append(f"{i3}<audio>")
        if a.get("codec"):
            L.append(f"{i3}  <codec>{a['codec']}</codec>")
        if a.get("language"):
            L.append(f"{i3}  <language>{a['language']}</language>")
        if a.get("channels"):
            L.append(f"{i3}  <channels>{a['channels']}</channels>")
        L.append(f"{i3}</audio>")
    for s in info.get("subtitles") or []:
        L.append(f"{i3}<subtitle>")
        if s.get("language"):
            L.append(f"{i3}  <language>{s['language']}</language>")
        L.append(f"{i3}</subtitle>")
    L += [f"{i2}</streamdetails>", f"{i1}</fileinfo>"]
    return "\n".join(L)


# 片源标记: TMM 的 <source> 也是从原始文件名猜的
# 边界用文件头定义的 _LB/_RB(不用 \b, 理由见文件头): '中字WEB-DL' 也要能猜出来。
SOURCE_HINTS = [
    (rf"{_LB}remux{_RB}", "REMUX"),
    (rf"{_LB}blu-?ray{_RB}|{_LB}bdrip{_RB}|{_LB}bdmv{_RB}|{_LB}complete\s*bluray{_RB}", "BLURAY"),
    (rf"{_LB}web-?dl{_RB}|{_LB}webrip{_RB}|{_LB}web{_RB}", "WEBDL"),
    (rf"{_LB}hdtv{_RB}", "HDTV"),
    (rf"{_LB}dvd{_RB}|{_LB}dvdrip{_RB}", "DVD"),
    (rf"{_LB}uhd{_RB}", "UHD"),
    (rf"{_LB}hddvd{_RB}", "HDDVD"),
    (rf"{_LB}tv{_RB}", "TV"),
]


def guess_source(name):
    """从原始文件名/目录名猜片源(REMUX/BLURAY/WEBDL/HDTV…),看不出返回 NONE。"""
    s = (name or "").lower()
    for pat, val in SOURCE_HINTS:
        if re.search(pat, s):
            return val
    return "NONE"


def guess_edition(name):
    """从名字猜版本(导演剪辑/加长版等);TMM 默认 NONE。

    边界同 SOURCE_HINTS(_LB/_RB): `\b` 在 CJK 相邻处不成立, '中字IMAX' 会猜不出来。
    """
    s = (name or "").lower()
    if re.search(rf"{_LB}extended{_RB}|加长", s):
        return "EXTENDED"
    if re.search(rf"{_LB}director'?s?\.?cut{_RB}|导演剪辑", s):
        return "DIRECTORSCUT"
    if re.search(rf"{_LB}unrated{_RB}|未分级", s):
        return "UNRATED"
    if re.search(rf"{_LB}remastered{_RB}|修复版", s):
        return "REMASTERED"
    if re.search(rf"{_LB}imax{_RB}", s):
        return "IMAX"
    if re.search(rf"{_LB}\d+\s*fps{_RB}|帧率", s):
        return "NONE"  # 帧率版本 TMM 也归 NONE
    return "NONE"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 lib/mediainfo.py <视频文件>")
        raise SystemExit(1)
    mi = probe_file(sys.argv[1])
    if not mi:
        print("探测失败:", sys.argv[1])
        raise SystemExit(1)
    print("分辨率 :", mi["video"]["resolution_label"], f"({mi['video']['width']}x{mi['video']['height']})",
          "aspect", mi["video"]["aspect"], "profile", mi["video"]["profile"])
    print("时长   :", mi["duration_secs"], "秒")
    print("视频   :", mi["video"]["codec"], "/ 文件名标记:", mi["video"]["codec_label"])
    for a in mi["audios"]:
        print(f"音频   : {a['codec']} ({a['codec_label']}) {a['channels']}ch lang={a['language']} title={a['title']!r}")
    for s in mi["subtitles"][:8]:
        print(f"字幕   : {s['language']} {s['codec']} forced={s['forced']}")
    print("质量标记:", quality_tag(mi))
    print("片源猜测:", guess_source(os.path.basename(sys.argv[1])), "| 版本:", guess_edition(os.path.basename(sys.argv[1])))
    print()
    print(streamdetails_xml(mi))
