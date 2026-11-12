import os

import structlog
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

logger = structlog.get_logger(__name__)

# 库角色四档（spec .scratch/db-dev-suffix D3）：只进日志，不做启动硬失败。
KNOWN_DB_ROLES = ("dev", "staging", "prod", "unknown")


def derive_db_role(path: str) -> str:
    """由库文件名推导角色：`_dev.db`→dev、`_staging.db`→staging、`person_fin.db`→prod、其余→unknown。

    兜底 unknown 而非 prod：临时副本/测试夹具/归档文件不得冒认生产库
    （业主 2026-11-09 裁决）。
    """
    name = os.path.basename(path or "")
    if name.endswith("_dev.db"):
        return "dev"
    if name.endswith("_staging.db"):
        return "staging"
    if name == "person_fin.db":
        return "prod"
    return "unknown"


def resolve_db_role(path: str, env_value: str | None = None) -> str:
    """`DB_ROLE` 环境变量优先；非法值原样透出并告警（不硬失败，见 D3）。"""
    raw = os.environ.get("DB_ROLE") if env_value is None else env_value
    if raw is not None and raw.strip():
        if raw not in KNOWN_DB_ROLES:
            logger.warning(
                "db_role_unrecognized",
                db_role=raw,
                db_path=path,
                known_roles=",".join(KNOWN_DB_ROLES),
            )
        return raw
    return derive_db_role(path)


# 支持从环境变量读取数据库路径，便于 Docker 配置
# 开发侧文件名固定带 `_dev` 后缀（部署侧仍为 person_fin.db，见 spec D1/D2）
DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "person_fin_dev.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"
DB_ROLE = resolve_db_role(DB_PATH)

logger.info("database_path", path=DB_PATH, role=DB_ROLE)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
