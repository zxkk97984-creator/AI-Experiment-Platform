from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def create_engine_from_url(database_url: str, *, settings=None):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    settings = settings or get_settings()
    pool_options = {}
    if not database_url.startswith("sqlite"):
        # 连接池必须有界且足够大：默认 5+10 会在高并发下排队到超时（见容量验收记录）。
        pool_options = {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_timeout": settings.db_pool_timeout,
            "pool_recycle": settings.db_pool_recycle,
        }
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True,
                           **pool_options)
    if database_url.startswith("sqlite"):
        # SQLite 默认不强制外键（PRAGMA foreign_keys 默认 OFF），导致测试里
        # 硬编码父 ID 的用例在 SQLite 上「假绿」、到 MySQL 上才 1452 失败。
        # 每个新连接开启外键，让 SQLite 行为与 MySQL 对齐（dev 与测试均受益）。
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def sessionmaker_for_engine(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


engine = create_engine_from_url(get_settings().database_url)
SessionLocal = sessionmaker_for_engine(engine)


def get_db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
