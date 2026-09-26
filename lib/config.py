#!/usr/bin/env python3
"""共享配置加载 —— 真相源是 SQLite 的 app_config 表(运行期【只读数据库】)

读取规则:
  load_config() 只读 DB 的 app_config['config'], 没有任何文件回退链。
  DB 里还没有配置这一行时(全新库), bootstrap() 做【一次性】初始化:
      config/config.json 或 <项目根>/config.json 存在 → 导入 DB(此后该文件永久失效)
      否则                              → 从 config.example.json 播种
  初始化完再读一次 DB。因此: 运行期改任何 JSON 文件都不会生效, 改配置只能走
  Web「管理 → 通用」页(或导入接口)。

磁盘上只有两类 JSON 会被读:
  1. bootstrap() 的一次性导入 —— 仅当 DB 没有配置这一行, 只发生一次;
  2. Web「通用」页的 导入/导出 —— 用户显式点击。

DB 位置(不再从任何配置文件读 db.path, 鸡生蛋到此为止):
  MEDIA_AUTO_DB > 默认 <项目根>/data/media_auto.db

⚠️ 本模块被 db/database.py 依赖(取 project_root), 所以【不能在模块级 import db.*】,
   所有数据库访问都在函数内惰性 import。
"""
import copy
import json
import os
import sys

_CONFIG_KEY = "config"
_SETUP_KEY = "setup_done"


def project_root():
    # lib/ -> 项目根目录
    return os.environ.get("MEDIA_AUTO_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def import_source_path():
    """【一次性导入源】的路径, 只在 bootstrap() 里用一次, 不参与运行期读取。

    查找顺序: MEDIA_AUTO_CONFIG > config/config.json(Docker 挂 ./config 目录) >
    <项目根>/config.json(本地旧布局) > 默认 config/config.json。
    """
    env = os.environ.get("MEDIA_AUTO_CONFIG")
    if env:
        return env if os.path.isabs(env) else os.path.join(project_root(), env)
    root = project_root()
    std = os.path.join(root, "config", "config.json")
    legacy = os.path.join(root, "config.json")
    if os.path.exists(std):
        return std
    if os.path.exists(legacy):
        return legacy
    return std


def db_path():
    """SQLite 路径: env > 默认。不碰数据库, 也不读任何配置文件。"""
    env = os.environ.get("MEDIA_AUTO_DB")
    if env:
        return env
    return os.path.join(project_root(), "data", "media_auto.db")


def _read_file(path):
    """读 JSON 文件; 不存在/语法错都返回 None(调用方区分不了也不需要区分)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[config] 读取 {path} 失败: {e}", file=sys.stderr)
        return None


def _example_path():
    return os.path.join(project_root(), "config.example.json")


def _db_read(key):
    """读 app_config 一行 → (value, updated_at)。任何失败返回 (None, 0)。"""
    try:
        from db.database import SessionLocal  # noqa: PLC0415  惰性: 避免循环依赖
        from db import repositories as repo  # noqa: PLC0415
        s = SessionLocal()
        try:
            if key == _CONFIG_KEY:
                data, ts = repo.get_app_config(s, key)
                return data, ts
            return repo.get_app_setting(s, key, None), 0
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001  表还没建 / DB 不可读
        if os.environ.get("MEDIA_AUTO_DEBUG"):
            print(f"[config] 读 DB[{key}] 失败: {e}", file=sys.stderr)
        return None, 0


def config_stamp():
    """热加载依据: DB 的 updated_at(没有则 0)。"""
    _, ts = _db_read(_CONFIG_KEY)
    return ts or 0


def _defaults():
    """config.example.json 的默认值(播种模板, 不是运行期配置源)。"""
    return _read_file(_example_path()) or {}


def load_config():
    """取配置: 只读数据库。

    库里还没有配置 → 先跑一次 bootstrap() 初始化(一次性导入 / 播种), 再读。
    初始化仍失败(库只读等)→ 返回 example 默认值, 不抛错, 免得整个进程起不来。
    """
    data, _ = _db_read(_CONFIG_KEY)
    if data is None:
        bootstrap()
        data, _ = _db_read(_CONFIG_KEY)
    if data is None:
        return _defaults()
    return copy.deepcopy(data)


def save_config(data: dict) -> int:
    """整份配置写 DB(原子事务)。返回 updated_at。表不存在时先建表再写。"""
    import json as _json
    if not isinstance(data, dict) or not data:
        raise ValueError("配置内容必须是非空 dict")
    _json.dumps(data, ensure_ascii=False)  # 提前暴露不可序列化问题
    return _write(_CONFIG_KEY, _json.dumps(data, ensure_ascii=False, indent=2))


def _write(key: str, value: str) -> int:
    try:
        from db.database import SessionLocal  # noqa: PLC0415
        from db import repositories as repo  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"数据库不可用: {e}") from e
    try:
        ts = _persist(repo, SessionLocal, key, value)
    except Exception:
        # 首次运行表还没建(init_db 在 server 启动时才调) → 建表后重试一次
        try:
            from db.database import init_db  # noqa: PLC0415
            init_db()
            ts = _persist(repo, SessionLocal, key, value)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"写入配置失败: {e}") from e
    return ts


def _persist(repo, sessionmaker, key, value):
    import time as _time
    s = sessionmaker()
    try:
        if key == _CONFIG_KEY:
            # 传 dict: set_app_config 自己序列化
            data = json.loads(value)
            ts = repo.set_app_config(s, data, key=key)
        else:
            repo.set_app_setting(s, key, value)
            ts = int(_time.time())
        s.commit()
        return ts
    finally:
        s.close()


def is_setup_done() -> bool:
    """首次引导是否已完成(从已有配置文件导入过 = 已完成, 不重走引导)。"""
    v, _ = _db_read(_SETUP_KEY)
    if v in ("1", "0"):
        return v == "1"
    # 没有标记: 只要 DB 里已有配置(非空壳)就算完成
    data, _ = _db_read(_CONFIG_KEY)
    return data is not None


def set_setup_done(done: bool) -> None:
    _write(_SETUP_KEY, "1" if done else "0")


def bootstrap() -> str:
    """首次初始化(幂等): 只在 DB 没有配置这一行时动文件, 且只动这一次。

    返回做了什么:
      'exists'  DB 已有配置, 什么都没做(文件从此与系统无关)
      'import'  从配置文件一次性导入 DB(老部署 → setup_done=1, 不重走引导)
      'seed'    从 config.example.json 播种(全新部署 → setup_done=0, 走首次引导)
    """
    data, _ = _db_read(_CONFIG_KEY)
    if data is not None:
        return "exists"

    src = import_source_path()
    src_data = _read_file(src)
    from_example = False
    if src_data is None:
        src = _example_path()
        src_data = _read_file(src)
        from_example = True
    if src_data is None:
        print("[config] 既无可导入的配置文件也无 config.example.json, 跳过初始化", file=sys.stderr)
        return "exists"

    src_data.pop("db", None)  # db.path 已随鸡生蛋一起退役(MEDIA_AUTO_DB / 默认路径)
    try:
        save_config(src_data)
        set_setup_done(not from_example)
    except Exception as e:  # noqa: BLE001  库不可写 → 不致命, 交给 load_config 的 example 兜底
        print(f"[config] 初始化写库失败: {e}", file=sys.stderr)
        return "exists"

    if from_example:
        print(f"[config] 首次启动: 已从 {src} 播种到数据库(setup_done=0, 走首次引导)")
    else:
        print(f"[config] 首次启动: 已从 {src} 一次性导入数据库"
              f"(setup_done=1; {src} 此后失效, 改配置请用 Web「管理 → 通用」)")
    return "seed" if from_example else "import"
