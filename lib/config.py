#!/usr/bin/env python3
"""共享配置加载 —— 真相源是 SQLite 的 app_config 表(运行期【只读数据库】)

配置只有两个入口:
  1. 首次初始化引导(Web setup wizard)—— 全新部署填必填项, 写进 DB;
  2. 「管理 → 通用」页 —— 日常增删改, 保存即热加载。
  没有任何配置文件参与: 不读 config.json, 也不做"首启从文件导入"。

bootstrap() 只在 DB 里还没有配置这一行时, 用 config.example.json 播种一份默认值
(setup_done=0 → 立刻进初始化引导), 然后由引导把它覆盖成真实配置。

DB 位置(不从任何文件读 db.path, 鸡生蛋到此为止):
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
# 已随功能退役的配置段: 保存/导入时自动剥离, 存量库里的残留也一并清掉
_DEAD_KEYS = ("tinymediamanager", "db")
# 退役的组内字段: (父段, 字段) —— tinyMediaManager 停用后 <tmm_locked/> 标签一并停写
_DEAD_FIELDS = (("organize", "tmm_locked"),)


def project_root():
    # lib/ -> 项目根目录
    return os.environ.get("MEDIA_AUTO_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



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
    for k in _DEAD_KEYS:
        data.pop(k, None)
    for parent, k in _DEAD_FIELDS:
        sec = data.get(parent)
        if isinstance(sec, dict):
            sec.pop(k, None)
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
    """首次初始化(幂等): 只在 DB 没有配置这一行时播种默认值。

    返回做了什么:
      'exists'  DB 已有配置, 什么都没做
      'seed'    用 config.example.json 播种默认值(setup_done=0 → 进首次初始化引导)
    """
    data, _ = _db_read(_CONFIG_KEY)
    if data is not None:
        return "exists"

    src = _example_path()
    src_data = _read_file(src)
    if src_data is None:
        print(f"[config] 找不到播种模板 {src}, 跳过初始化", file=sys.stderr)
        return "exists"

    try:
        save_config(src_data)
        set_setup_done(False)   # 播种的是默认值 → 必须走首次初始化引导
    except Exception as e:  # noqa: BLE001  库不可写 → 不致命, 交给 load_config 的 example 兜底
        print(f"[config] 初始化写库失败: {e}", file=sys.stderr)
        return "exists"

    print(f"[config] 首次启动: 已用 {src} 播种默认配置(setup_done=0, 进入首次初始化引导)")
    return "seed"
