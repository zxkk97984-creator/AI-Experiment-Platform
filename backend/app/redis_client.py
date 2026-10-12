"""API 侧 Redis 客户端工厂——有界超时、零自动重试、固定连接池上限。

约定（见实施计划批次四）：

- 连接与读取各 0.5 秒，0 次自动重试：认证路径宁可快速失败返回 503，
  也不允许请求在故障 Redis 上堆积；
- 连接池上限 32，避免故障期间连接数无界增长；
- 指标使用**独立**客户端（连接/读取各 0.1 秒），不能与业务请求共用超时；
- 阻塞队列 Worker 使用各自独立的客户端策略，不复用本模块。
"""

from __future__ import annotations

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

API_CONNECT_TIMEOUT_SECONDS = 0.5
API_READ_TIMEOUT_SECONDS = 0.5
API_MAX_CONNECTIONS = 32

METRICS_CONNECT_TIMEOUT_SECONDS = 0.1
METRICS_READ_TIMEOUT_SECONDS = 0.1


def _no_retry() -> Retry:
    """零自动重试：Redis 故障时立即失败，由调用方决定 503 语义。"""
    return Retry(NoBackoff(), 0)


def build_api_redis(settings, *, decode_responses: bool = True) -> Redis:
    return Redis.from_url(
        settings.redis_url,
        decode_responses=decode_responses,
        socket_connect_timeout=API_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=API_READ_TIMEOUT_SECONDS,
        retry=_no_retry(),
        retry_on_timeout=False,
        retry_on_error=[],
        max_connections=API_MAX_CONNECTIONS,
    )


def build_metrics_redis(settings, *, decode_responses: bool = True) -> Redis:
    """指标专用客户端：更短超时，失败即丢弃本批指标。"""
    return Redis.from_url(
        settings.redis_url,
        decode_responses=decode_responses,
        socket_connect_timeout=METRICS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=METRICS_READ_TIMEOUT_SECONDS,
        retry=_no_retry(),
        retry_on_timeout=False,
        retry_on_error=[],
        max_connections=4,
    )
