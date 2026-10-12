"""Run ``python -m app.worker.kernel_worker`` on the Docker execution node.

数据库是唯一事实源；Redis 只用于唤醒空闲轮询，Redis 故障时降级为 5s 轮询。
每次执行独立线程 + 独立租约心跳，终态写入被 lease_token 栅栏保护：worker 崩溃后
recover 会重新排队，旧 worker 永远无法覆盖新结果。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import redis
from sqlalchemy import func, select, update

from app.config import get_settings
from app.database import SessionLocal
from app.models import ExperimentRecord, NotebookTemplateVersion
from app.models.kernel_exec import KernelExecution
from app.services.kernel_queue import (
    ClaimScanState,
    ExecutionCancelled,
    KernelLeaseGuard,
    LeaseLost,
    claim_execution,
    finish_execution,
    recover_executions,
)

logger = logging.getLogger("dai.kernel_worker")


def _resolve_run_environment(db, record):
    """记录快照绑定的镜像 digest + 环境版本（与 API 准入路径同一解析规则）。"""
    env_id = getattr(record, "environment_version_id", None)
    image_ref = None
    if env_id is not None:
        from app.services.environment_service import resolve_run_image_ref

        try:
            image_ref = resolve_run_image_ref(db, env_id)
        except Exception as exc:
            raise RuntimeError("运行环境暂不可用，请稍后重试") from exc
    return image_ref, env_id


def _interrupt_kernel(manager, record_id):
    try:
        manager.interrupt(record_id)
    except Exception:
        logger.warning("中断 kernel record=%s 的当前执行失败", record_id, exc_info=True)


def _prepare_kernel(session_factory, manager, execution_id, token):
    """在执行前确保内核与隐藏脚手架就绪（worker 可能面对刚重建的内核）。"""
    from app.services.lab_workspace import initialize_hidden_cells

    with session_factory() as db:
        execution = db.get(KernelExecution, execution_id)
        if execution is None:
            raise LeaseLost("Kernel execution disappeared")
        record = db.get(ExperimentRecord, execution.record_id)
        if record is None:
            raise RuntimeError("实验记录不存在")
        version = db.get(NotebookTemplateVersion, record.template_version_id)
        image_ref, env_id = _resolve_run_environment(db, record)
        initialize_hidden_cells(manager, record, version, db, image_ref=image_ref,
                                environment_version_id=env_id,
                                timeout_seconds=execution.timeout_seconds)
        return record.id


def _admission_context_changed(record, context) -> bool:
    if not context:
        return False
    return (record.record_revision != context.get("record_revision")
            or record.offering_id != context.get("offering_id")
            or record.student_id != context.get("student_id")
            or record.template_version_id != context.get("template_version_id")
            or getattr(record, "environment_version_id", None)
            != context.get("environment_version_id"))


def _record_output_payload(status, result, execution_count):
    return {
        "execution_count": execution_count,
        "outputs": result.get("outputs") or [],
        "execution_time_ms": result.get("execution_time_ms"),
        # 刷新后仍能看到「超时/已中断/状态丢失」的事实，不把未完成状态当完整结果
        "status": status,
        "state_lost": bool(result.get("state_lost")),
        "error_message": result.get("message"),
    }


def _finalize(db, execution_id, token, result, status, error_code):
    """终态 CAS + record.cells_outputs 同一事务落库；失败即回滚（不写半结果）。"""
    from app.services.import_policy import ImportPolicy
    from app.services.kernel_result import (
        classify_kernel_module_not_found,
        next_visible_execution_count,
        replace_error_output,
    )

    execution = db.get(KernelExecution, execution_id, populate_existing=True)
    if execution is None or execution.status != "running" or execution.lease_token != token:
        return False
    record = db.get(ExperimentRecord, execution.record_id)
    if record is None:
        return False
    if _admission_context_changed(record, execution.context):
        finish_execution(db, execution_id, token, "failed", error_code="REVISION_CONFLICT",
                         error_message="作答或授课归属在运行期间发生变化，请刷新后重新运行")
        db.commit()
        return True

    version = db.get(NotebookTemplateVersion, record.template_version_id)
    outputs = list(result.get("outputs") or [])
    env_id = getattr(record, "environment_version_id", None)
    if env_id is not None and version is not None:
        from app.services.environment_service import installed_imports_for_version

        policy = ImportPolicy.from_mode(
            version.import_policy_mode, list(version.allowed_imports or []))
        try:
            installed = installed_imports_for_version(db, env_id)
        except Exception:
            installed = set()
        diagnostic = classify_kernel_module_not_found(outputs, policy, installed)
        if diagnostic is not None:
            outputs = replace_error_output(outputs, diagnostic.message)
    result = dict(result, outputs=outputs)

    execution_count = next_visible_execution_count(record, version)
    changed = finish_execution(
        db, execution_id, token, status,
        outputs=result.get("outputs"),
        execution_time_ms=result.get("execution_time_ms"),
        execution_count=execution_count,
        error_code=error_code,
        error_message=result.get("message"),
        state_lost=bool(result.get("state_lost")),
    )
    if not changed:
        db.rollback()
        return False
    cells_outputs = dict(record.cells_outputs or {})
    cells_outputs[execution.cell_id] = _record_output_payload(status, result, execution_count)
    record.cells_outputs = cells_outputs
    db.commit()
    return True


def _abandon(db, execution_id, token):
    """基础设施异常：让租约立刻过期，由 recover 重新排队（不猜结果、不写 0）。"""
    now = db.scalar(select(func.now()))
    db.execute(update(KernelExecution).where(
        KernelExecution.id == execution_id,
        KernelExecution.lease_token == token,
        KernelExecution.status == "running",
    ).values(lease_until=now).execution_options(synchronize_session=False))
    db.commit()


def process_execution(session_factory, execution_id, token, settings):
    """执行一次已认领的单元格任务（独立线程内运行）。"""
    from app.services.kernel_manager import get_kernel_manager
    from app.services.lab_workspace import execute_kernel_code

    manager = get_kernel_manager()
    record_id = None
    try:
        with session_factory() as db:
            execution = db.get(KernelExecution, execution_id)
            if (execution is None or execution.status != "running"
                    or execution.lease_token != token):
                return
            record_id = execution.record_id
            code = execution.code
            timeout_seconds = execution.timeout_seconds

        with KernelLeaseGuard(session_factory, execution_id, token,
                              on_cancel=lambda: _interrupt_kernel(manager, record_id)) as guard:
            guard.assert_owned()
            _prepare_kernel(session_factory, manager, execution_id, token)
            result = execute_kernel_code(manager, record_id, code,
                                         timeout_seconds=timeout_seconds,
                                         interrupt_event=guard.cancel_event)
            outcome = result.get("outcome")
            if outcome == "succeeded":
                status, error_code = "succeeded", None
            elif outcome == "timeout":
                status, error_code = "timeout", "CELL_TIMEOUT"
            elif outcome == "cancelled":
                status, error_code = "cancelled", "CANCELLED"
            else:
                status = "failed"
                error_code = "KERNEL_UNRESPONSIVE" if result.get("state_lost") else "KERNEL_ERROR"
            with session_factory() as db:
                _finalize(db, execution_id, token, result, status, error_code)
    except LeaseLost:
        logger.info("执行 %s 失去租约，交由恢复逻辑重新排队", execution_id)
    except ExecutionCancelled:
        with session_factory() as db:
            _finalize(db, execution_id, token, {"outputs": [], "message": "已中断本次执行"},
                      "cancelled", "CANCELLED")
    except Exception:
        logger.exception("执行 %s 异常中断；由持久化恢复重试", execution_id)
        with session_factory() as db:
            execution = db.get(KernelExecution, execution_id, populate_existing=True)
            if execution is not None and execution.cancel_requested:
                _finalize(db, execution_id, token,
                          {"outputs": [], "message": "已中断本次执行"}, "cancelled", "CANCELLED")
            else:
                # 永不猜测结果或提前释放：让租约过期，recover 决定重排还是失败
                _abandon(db, execution_id, token)


def main():
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                  socket_connect_timeout=2, socket_timeout=3)
    subscription = None
    futures = set()
    scan = ClaimScanState()
    concurrency = max(1, settings.lab_max_kernels)
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="kernel-exec")
    try:
        while True:
            futures = {future for future in futures if not future.done()}
            try:
                with SessionLocal() as db:
                    recover_executions(db, settings)
                claimed = None
                if len(futures) < concurrency:
                    with SessionLocal() as db:
                        claimed = claim_execution(db, settings, scan)
                if claimed:
                    futures.add(pool.submit(process_execution, SessionLocal, *claimed, settings))
                    continue
                if subscription is None:
                    subscription = client.pubsub(ignore_subscribe_messages=True)
                    subscription.subscribe("kernel-executions")
                subscription.get_message(timeout=min(5, settings.lab_queue_poll_seconds))
            except redis.exceptions.RedisError:
                # Redis 只做唤醒：故障时退化为轮询，任务仍在数据库里
                if subscription is not None:
                    try:
                        subscription.close()
                    except redis.exceptions.RedisError:
                        pass
                subscription = None
                time.sleep(min(5, settings.lab_queue_poll_seconds))
            except Exception:
                logger.exception("Kernel worker 轮询失败；任务仍留在数据库中")
                time.sleep(min(5, settings.lab_queue_poll_seconds))
    except KeyboardInterrupt:
        logger.info("Kernel worker 正在关闭，等待当前执行结束")
    finally:
        if subscription is not None:
            subscription.close()
        client.close()
        pool.shutdown(wait=True)


if __name__ == "__main__":
    main()
