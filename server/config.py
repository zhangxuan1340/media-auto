"""MediaAuto Web —— 配置与路径

配置真相源是 SQLite 的 app_config 表(见 db/models.AppConfig 与 lib/config.py):
  get_config()   按 DB 的 updated_at 自动热加载, 每次请求都读到最新值
  save_config()  统一写入口(Web「通用」页 / 分类规则 / qbit 配置都走它)

运行期【只读数据库】: 本模块不读任何配置文件, 配置文件只在首启 lib.config.bootstrap()
被一次性导入(此后失效)。state/queue.json 固定在 PROJECT_ROOT/state/,与配置文件无关。
"""
import copy
import sys
from pathlib import Path

# server/ -> 项目根目录(workspace root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = PROJECT_ROOT  # 兼容旧引用

# 把项目根目录加入 sys.path,这样能直接 import lib / clients / scripts / db
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib import config as lib_config  # noqa: E402  (依赖上面的 sys.path 注入)


def load_config():
    """读配置(只读数据库; 全库唯一实现见 lib.config.load_config)。"""
    return lib_config.load_config()


# 模块级配置(进程启动时加载一次); bootstrap 后由 main 调 reload_config() 刷新
CONFIG = load_config()

_CONFIG_CACHE = {"stamp": None, "data": None}
_config_cache = None


def _stamp():
    """热加载依据: 只有 DB 的 updated_at(文件 mtime 已随配置退役, 不再参与)。"""
    try:
        return lib_config.config_stamp()
    except Exception:  # noqa: BLE001
        return 0


def get_config():
    """FastAPI 依赖: 返回配置字典(副本, 防 handler 误改污染全局缓存)。

    按 updated_at 自动重载: Web 页改配置 / 分类规则写回后, 下一次请求即读到
    新值, 不需要重启容器(一次索引查询, 无性能负担)。
    """
    global _config_cache
    m = _CONFIG_CACHE
    st = _stamp()
    if st != m["stamp"]:
        m["data"] = load_config()
        m["stamp"] = st
        _config_cache = m["data"]
    return copy.deepcopy(m["data"] or {})


def reload_config():
    """强制重读配置(写库后立刻调用, 不等下一次请求的 stamp 检查)。"""
    global _config_cache
    _CONFIG_CACHE["data"] = load_config()
    _CONFIG_CACHE["stamp"] = _stamp()
    _config_cache = _CONFIG_CACHE["data"]
    return _config_cache


def save_config(data: dict):
    """整份配置写 DB + 立即刷新本进程缓存(其余进程/下次请求按 stamp 自动跟上)。"""
    lib_config.save_config(data)
    return reload_config()


def skill_root():
    return str(PROJECT_ROOT)
