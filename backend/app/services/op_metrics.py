"""TASK-029（F-36）：最小运维指标——固定指标集 + Redis 计数器，不绑定监控厂商。

设计约束：
- 指标名与标签值均为固定白名单（防高基数：绝不把用户 id/题目 id/路径原文当标签）；
- **每进程一个固定大小聚合器**：``record`` 只改内存计数（无 I/O），
  每秒由单个后台任务 flush 一次（最多一个 flush 在途），因此故障 Redis 不会
  让请求路径堆积；
- flush 失败丢弃该批并累计丢失计数（日志限频），不落盘、不重放、不新增队列；
- 窗口按整点小时，保留 25 小时；读取方（/metrics 等）取当前小时，
  并叠加尚未 flush 的内存计数，保证刚发生的请求也可见。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("dai.op_metrics")

RETENTION_HOURS = 25

# flush 周期（秒）与关闭时限（秒）
FLUSH_INTERVAL_SECONDS = 1.0
SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 1.0
# 同一指标行的丢失批次数（固定大小：只有白名单 (name,label) 组合）
_LOST_FLUSHES = 0
_LOST_LOG_EVERY = 60

# 固定大小聚合器：键空间 = METRIC_NAMES × ALLOWED_LABELS，天然有界
_PENDING: dict[tuple[str, str], int] = {}
_PENDING_LOCK = threading.Lock()
_FLUSH_TASK: asyncio.Task | None = None

# 固定指标名：新增指标必须在此登记（拒绝任意名称，防高基数/注入）
METRIC_NAMES = {
    "http_requests_total",          # 标签：2xx/3xx/4xx/5xx
    "http_latency_ms_sum",          # 标签：2xx/3xx/4xx/5xx（配合 requests 求平均）
    "judge_failures_total",         # 标签：permanent/retryable
    "ai_requests_total",            # 标签：ai_grading/rubric_generation/test_group_generation
    "ai_prompt_tokens_total",       # 标签：同上
    "ai_completion_tokens_total",   # 标签：同上
}

# 固定标签值：按指标名分组白名单
ALLOWED_LABELS: dict[str, set[str]] = {
    "http_requests_total": {"2xx", "3xx", "4xx", "5xx"},
    "http_latency_ms_sum": {"2xx", "3xx", "4xx", "5xx"},
    "judge_failures_total": {"permanent", "retryable"},
    "ai_requests_total": {"ai_grading", "rubric_generation", "test_group_generation"},
    "ai_prompt_tokens_total": {"ai_grading", "rubric_generation", "test_group_generation"},
    "ai_completion_tokens_total": {"ai_grading", "rubric_generation", "test_group_generation"},
}

_metrics_redis = None  # 惰性单例；模块级缓存避免每请求新建连接


def get_metrics_redis():
    """指标专用 Redis 客户端（连接/读取各 0.1 秒、零重试）；不可用返回 None。"""
    global _metrics_redis
    if _metrics_redis is None:
        try:
            from app.config import get_settings
            from app.redis_client import build_metrics_redis

            _metrics_redis = build_metrics_redis(get_settings())
        except Exception as exc:  # pragma: no cover - 环境问题
            logger.warning("指标 Redis 初始化失败，指标降级为 no-op: %s", exc)
            return None
    return _metrics_redis


def _window() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d%H")


def record(redis_client=None, name: str = "", value: int = 1, *, label: str | None = None) -> None:
    """指标自增。名称/标签不在白名单 → 拒绝（记警告，不落库）。

    **只写内存聚合器，不做任何 I/O**：因此 Redis 故障不会拖慢或阻塞业务请求。
    真实写入由每秒一次的 flush 任务完成（每进程一个，最多一个在途）。

    ``redis_client`` 仅作为兼容参数保留（调用点很多）；不再直接使用。
    """
    if name not in METRIC_NAMES:
        logger.warning("拒绝未登记指标名: %r", name)
        return
    allowed = ALLOWED_LABELS[name]
    if (label or "") not in allowed:
        logger.warning("拒绝指标 %s 的未登记标签: %r", name, label)
        return
    key = (name, label or "")
    with _PENDING_LOCK:
        _PENDING[key] = _PENDING.get(key, 0) + int(value)


def reset_pending() -> None:
    """清空内存聚合器（测试隔离与关闭清理用）。"""
    with _PENDING_LOCK:
        _PENDING.clear()


def pending_snapshot() -> dict[tuple[str, str], int]:
    with _PENDING_LOCK:
        return dict(_PENDING)


def flush_pending() -> int:
    """把内存计数写入 Redis（pipeline）；失败丢弃该批并计数，返回成功写入的行数。"""
    global _LOST_FLUSHES
    with _PENDING_LOCK:
        batch = dict(_PENDING)
        _PENDING.clear()
    if not batch:
        return 0
    client = get_metrics_redis()
    if client is None:
        _note_lost_batch(len(batch))
        return 0
    try:
        pipe = client.pipeline(transaction=False)
        for (name, label), value in batch.items():
            key = f"opmetrics:{name}:{label or 'none'}:{_window()}"
            pipe.incrby(key, value)
            pipe.expire(key, RETENTION_HOURS * 3600)
        pipe.execute()
        return len(batch)
    except Exception:
        _note_lost_batch(len(batch))
        return 0


# A15：Worker 进程没有事件循环/lifespan，无法使用 API 的每秒 flush 任务。
# 每个任务结束后调用一次，按最小间隔限频（默认 1s，与 API 侧口径一致）。
WORKER_FLUSH_MIN_INTERVAL_SECONDS = 1.0
_worker_last_flush = 0.0


def flush_for_worker(force: bool = False) -> int:
    """Worker 进程的指标出口：把内存聚合器写入 Redis，返回写入行数。

    * 由 Judge/AI Worker 在每个任务结束时调用（``finally`` 保证异常路径也冲刷）；
    * 按 ``WORKER_FLUSH_MIN_INTERVAL_SECONDS`` 限频，避免每任务一次 Redis 往返；
    * Redis 不可用时丢弃该批并计数（与 API 侧一致，绝不阻塞判题）。
    """
    global _worker_last_flush
    now = time.monotonic()
    if not force and now - _worker_last_flush < WORKER_FLUSH_MIN_INTERVAL_SECONDS:
        return 0
    _worker_last_flush = now
    return flush_pending()


def _note_lost_batch(rows: int) -> None:
    """丢弃一批并计数；日志限频，避免故障期间刷屏。"""
    global _LOST_FLUSHES
    _LOST_FLUSHES += 1
    if _LOST_FLUSHES % _LOST_LOG_EVERY == 1:
        logger.warning("指标 flush 失败，已丢弃 %s 行（累计丢失批次 %s）", rows, _LOST_FLUSHES)


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        # 阻塞 I/O 放到线程中，避免拖慢事件循环；同一时刻只有一个 flush 在途
        await asyncio.to_thread(flush_pending)


def start_flush_task() -> asyncio.Task:
    """在 API 进程内启动唯一的 flush 任务（lifespan 调用）。"""
    global _FLUSH_TASK
    if _FLUSH_TASK is None or _FLUSH_TASK.done():
        _FLUSH_TASK = asyncio.create_task(_flush_loop())
    return _FLUSH_TASK


async def stop_flush_task(timeout: float = SHUTDOWN_FLUSH_TIMEOUT_SECONDS) -> None:
    """关闭：先取消任务，再在时限内做一次 best-effort flush（不阻塞退出）。"""
    global _FLUSH_TASK
    task = _FLUSH_TASK
    _FLUSH_TASK = None
    if task is not None:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            pass
    try:
        await asyncio.wait_for(asyncio.to_thread(flush_pending), timeout=timeout)
    except Exception:
        pass


def _pending_for(name: str, label: str | None) -> int:
    with _PENDING_LOCK:
        return _PENDING.get((name, label or ""), 0)


def read(redis_client, name: str, *, label: str | None = None) -> int:
    """读取当前窗口计数（已 flush + 尚未 flush 的内存计数）；异常返回内存值。"""
    if name not in METRIC_NAMES:
        return 0
    pending = _pending_for(name, label)
    try:
        raw = redis_client.get(f"opmetrics:{name}:{label or 'none'}:{_window()}")
        return int(raw or 0) + pending
    except Exception:
        return pending


def snapshot(redis_client) -> dict[str, dict[str, int]]:
    """当前窗口全量快照（/metrics 端点用）。"""
    result: dict[str, dict[str, int]] = {}
    for name in sorted(METRIC_NAMES):
        result[name] = {
            label: read(redis_client, name, label=label)
            for label in sorted(ALLOWED_LABELS[name])
        }
    return result


def http_metrics_recorder():
    """给 API 中间件用的记录函数：只更新内存聚合器（无每请求 Redis I/O）。"""

    def _record(status_class: str, latency_ms: float) -> None:
        record(name="http_requests_total", label=status_class)
        record(name="http_latency_ms_sum", value=int(latency_ms), label=status_class)

    return _record


def ai_metrics_sink():
    """给 DeepSeekClient 的 metrics_sink：按操作累加 token 计数与调用次数。"""

    def _sink(data: dict) -> None:
        operation = data.get("operation", "")
        if operation not in ALLOWED_LABELS["ai_requests_total"]:
            return
        record(name="ai_requests_total", label=operation)
        if data.get("prompt_tokens"):
            record(name="ai_prompt_tokens_total", value=int(data["prompt_tokens"]), label=operation)
        if data.get("completion_tokens"):
            record(name="ai_completion_tokens_total", value=int(data["completion_tokens"]), label=operation)

    return _sink


def queue_depth(redis_client, queue_name: str) -> int:
    """Redis 队列当前深度；异常返回 -1（调用方显式报告不可用）。"""
    try:
        return int(redis_client.llen(queue_name))
    except Exception:
        return -1
