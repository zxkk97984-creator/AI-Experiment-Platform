"""数据库连接池配置回归（容量验收暴露的缺口）。

背景：默认 QueuePool(size=5, overflow=10) 在 100 并发学生下被瞬间打满，请求排队
30 秒并把整个 API 拖死（连 `/health/live` 都不响应）。现在连接池按配置建，
等待时间有界（快速失败而不是无限排队）。
"""
from __future__ import annotations

from sqlalchemy.pool import QueuePool

from app.config import Settings
from app.database import create_engine_from_url


def _settings(**overrides) -> Settings:
    base = {"database_url": "mysql+pymysql://u:p@127.0.0.1:3306/x"}
    base.update(overrides)
    return Settings(**base)


def test_mysql_engine_uses_configured_pool():
    engine = create_engine_from_url(_settings().database_url, settings=_settings())
    try:
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == 20
        status = engine.pool.status()
        assert "Pool size: 20" in status
        assert "Current Overflow" in status
    finally:
        engine.dispose()


def test_pool_settings_are_overridable():
    settings = _settings(db_pool_size=100, db_max_overflow=40, db_pool_timeout=5.0,
                         db_pool_recycle=1800)
    engine = create_engine_from_url(settings.database_url, settings=settings)
    try:
        assert engine.pool.size() == 100
        assert engine.pool._max_overflow == 40
        assert engine.pool._timeout == 5.0
    finally:
        engine.dispose()


def test_pool_timeout_is_bounded_not_infinite():
    """连接耗尽时必须快速失败：默认 30 秒排队会把请求线程全部占死。"""
    settings = _settings()
    assert 0 < settings.db_pool_timeout <= 10
    assert settings.db_pool_size + settings.db_max_overflow >= 60, "需为并发学生留出余量"


def test_sqlite_engine_keeps_default_behaviour(tmp_path):
    """SQLite（开发/测试）不套用 MySQL 的连接池参数，保持原行为。"""
    url = f"sqlite:///{tmp_path / 'pool.db'}"
    engine = create_engine_from_url(url, settings=_settings())
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    finally:
        engine.dispose()
