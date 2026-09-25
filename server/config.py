"""MediaAuto Web —— 配置与路径

集中加载 config.json,并暴露项目根目录,供后端复用 lib/、clients/、scripts/ 与 db/。
"""
import copy
import json
import os
import sys
from pathlib import Path

# server/ -> 项目根目录(workspace root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = PROJECT_ROOT  # 兼容旧引用

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

# 把项目根目录加入 sys.path,这样能直接 import lib / clients / scripts / db
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 同时支持从任意工作目录启动
WORKDIR = os.environ.get("MEDIA_AUTO_DIR", str(PROJECT_ROOT))
CONFIG_FILENAME = os.environ.get("MEDIA_AUTO_CONFIG", "config.json")
CONFIG_PATH = Path(WORKDIR) / CONFIG_FILENAME

_config_cache = None


def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# 模块级配置(进程启动时加载一次); 需要热更新时调用 reload_config()
CONFIG = load_config()

_CONFIG_CACHE = {"mtime": None, "data": None}


def get_config():
    """FastAPI 依赖: 返回配置字典(副本, 防 handler 误改污染全局缓存)。

    按 mtime 自动重载: config.json 被改动后, 下一次请求即读到新值
    (本地文件 stat 是微秒级, 每请求检查无性能负担)。这样运行时改配置,
    网页路由与后台同步读到的是同一份最新配置, 不再出现
    "路由用旧缓存、sync 用新值"的行为分裂。
    """
    global _config_cache  # 兼容旧引用
    m = _CONFIG_CACHE
    try:
        st_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        st_mtime = None
    if st_mtime != m["mtime"]:
        m["data"] = load_config()
        m["mtime"] = st_mtime
        _config_cache = m["data"]
    return copy.deepcopy(m["data"] or {})


def reload_config():
    """强制重读 config.json(立即生效, 不等下一次请求的 mtime 检查)。"""
    global _config_cache
    _CONFIG_CACHE["data"] = load_config()
    try:
        _CONFIG_CACHE["mtime"] = CONFIG_PATH.stat().st_mtime
    except OSError:
        _CONFIG_CACHE["mtime"] = None
    _config_cache = _CONFIG_CACHE["data"]
    return _config_cache


def skill_root():
    return str(PROJECT_ROOT)
