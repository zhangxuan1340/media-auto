"""SQLite 存储基础设施

本地 SQLite 作为 Seerr / Jellyfin 同步数据的落地库,以及同步日志。
数据库文件默认位于 <项目根>/data/media_auto.db,可用环境变量 MEDIA_AUTO_DB 覆盖。
"""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from lib.config import project_root

DEFAULT_DB_PATH = os.path.join(project_root(), "data", "media_auto.db")
DB_PATH = os.environ.get("MEDIA_AUTO_DB", DEFAULT_DB_PATH)

# 确保目录存在
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# check_same_thread=False: 同步引擎在 FastAPI 线程池里被多个线程共用
# timeout=30: sqlite3 的 busy_timeout 单位是**秒**(此处 30s), 默认仅 5s。
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _rec):
    """每个连接开 WAL + 拉长忙等, 修掉「同步作业把 HTTP 写请求挡死」。

    实测故障(2026-09-22): 同步作业刚启动时读 7.7 万分集建索引 → 长读事务持有
    SHARED 锁; 默认 `journal_mode=delete` 下**读者会挡住写者**, 此时点「可用性扫描」
    / 「触发器扫描」等接口会在 5s 后抛
    `sqlite3.OperationalError: database is locked` → HTTP 500。

    · `journal_mode=WAL`: 读写**不互斥**(读者不挡写者、写者不挡读者), 只剩"写-写"串行。
    · `busy_timeout=30000`(ms): 写-写撞车时**等待**而不是立刻放弃。
    · `synchronous=NORMAL`: WAL 下的推荐档, 崩溃最多丢最后一个事务, 不会损坏库。

    ⚠️ WAL 需要文件系统支持共享内存。本库在 `/Volumes/UData`(实测 mount 为
    `apfs, local`) ✓。若日后把 DB 挪到**网络盘**(SMB/NFS), 必须改回 delete 模式,
    否则会开库失败。
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
    finally:
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
Base = declarative_base()


def _sql_default(col):
    """为一个新增列推导一个可用于 ``ADD COLUMN ... DEFAULT`` 的 SQL 字面量。

    SQLite 的 ``ALTER TABLE ADD COLUMN`` 在列声明为 NOT NULL 时必须有默认值,
    这里按模型默认值 / 列类型兜底, 保证能顺利补列。
    """
    from sqlalchemy import Boolean, DateTime, Float, Integer, Numeric

    val = None
    if col.default is not None and col.default.is_scalar:
        val = col.default.arg
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(val)
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    # 取不到标量默认值(如 func.now())时按类型兜底
    t = col.type
    if isinstance(t, DateTime):
        return "CURRENT_TIMESTAMP"
    if isinstance(t, Integer):
        return "0"
    if isinstance(t, Boolean):
        return "0"
    if isinstance(t, (Float, Numeric)):
        return "0.0"
    return "''"


def _migrate_missing_columns():
    """为已存在的表补齐模型新增的列(SQLite 的 create_all 不会 ALTER 旧表)。

    幂等: 逐表比对 ``PRAGMA table_info`` 与模型列, 只 ``ALTER TABLE ADD COLUMN``
    缺失的列。这样"给已建表加字段"不会再导致 ``no such column`` 的运行时错误。
    """
    from sqlalchemy import text

    added = []
    with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            rows = conn.execute(text(f"PRAGMA table_info({table.name})")).fetchall()
            existing = {r[1] for r in rows}
            for col in table.columns:
                if col.name in existing:
                    continue
                type_sql = col.type.compile(dialect=engine.dialect)
                if col.nullable:
                    # 可空列: 省略 DEFAULT(SQLite ADD COLUMN 不允许非常量默认如
                    # CURRENT_TIMESTAMP), 列自动默认 NULL。
                    ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {type_sql}"
                else:
                    not_null = "NOT NULL"
                    default = _sql_default(col)
                    ddl = (f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                           f"{type_sql} {not_null} DEFAULT {default}".strip())
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added


def init_db():
    """建表(幂等)。首次启动、新增表、或给旧表加字段时调用。"""
    import db.models  # noqa: F401  确保模型已注册到 Base.metadata
    Base.metadata.create_all(bind=engine)
    _migrate_missing_columns()
    return DB_PATH


def get_db():
    """FastAPI 依赖: 返回一个会话并在请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
