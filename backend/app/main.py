import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import api_router
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.dependencies import get_db, get_redis_client, require_roles
from app.services.exam_service import scan_expired_exams
from app.services.op_metrics import http_metrics_recorder

logger = logging.getLogger("dai.main")

# TASK-029：HTTP 状态类别/延迟指标记录器（Redis 故障时内部 no-op，不阻断请求）
_http_metrics = http_metrics_recorder()


async def _run_sync_in_thread(func, *args, **kwargs):
    """Run blocking maintenance work without occupying the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _scan_expired_exams_once(owner_id: str):
    from app.services.scheduler_lease import try_acquire_lease
    from app.services.time_utils import utc_now

    with SessionLocal() as db:
        if not try_acquire_lease(db, "exam-expiry", owner_id, ttl_seconds=20):
            return None
        return scan_expired_exams(db, utc_now())


def _cleanup_kernel_once(owner_id: str):
    from app.config import get_settings
    from app.services.kernel_manager import get_kernel_manager
    from app.services.scheduler_lease import try_acquire_lease

    with SessionLocal() as db:
        # 多 API 实例下同一时刻只有一个实例执行内核清理（本协程此前是唯一无租约的后台任务）
        if not try_acquire_lease(db, "kernel-cleanup", owner_id, ttl_seconds=240):
            return None
        # 有在途执行的内核不会被回收（cleanup_idle 内部查 kernel_executions）
        get_kernel_manager().cleanup_idle(
            max_idle_seconds=get_settings().kernel_idle_seconds, db=db
        )


def _normalize_detail(detail):
    if isinstance(detail, dict) and set(detail.keys()) == {"code", "message", "fields"}:
        return detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        normalized = {
            "code": detail["code"],
            "message": detail["message"],
            "fields": detail.get("fields", {}),
        }
        if "field_errors" in detail:
            normalized["field_errors"] = detail["field_errors"]
        if "retryable" in detail:
            normalized["retryable"] = detail["retryable"]
        return normalized
    if isinstance(detail, str):
        return {"code": "ERROR", "message": detail, "fields": {}}
    return {"code": "ERROR", "message": str(detail), "fields": {}}


async def _expiry_scanner():
    """定期扫描：过期考试自动交卷 + grading finalize（约 5 秒一轮）。

    多 API 实例下通过 exam-expiry DB 租约保证同一时刻只有一个实例执行；
    judge/AI stale recovery 由 Judge Worker 在 grading-recovery 租约下负责，
    此处不再重复做判题恢复。
    """
    import os
    import socket
    import uuid
    # owner 每进程唯一：hostname + pid + 随机实例 ID
    # （同机多 API 进程若只含 hostname 会共享 owner，导致同时续租同时扫描）
    _instance_id = uuid.uuid4().hex[:8]
    owner_id = f"api:{socket.gethostname()}:{os.getpid()}:{_instance_id}"
    while True:
        try:
            await asyncio.sleep(5)
            metrics = await _run_sync_in_thread(_scan_expired_exams_once, owner_id)
            if metrics and any(v > 0 for v in metrics.values()):
                logger.info("考试扫描: %s", metrics)
        except Exception:
            logger.exception("过期考试扫描异常")


async def _kernel_cleanup():
    """定期清理空闲 Kernel session（多副本 DB 租约互斥）"""
    import os
    import socket
    import uuid

    owner_id = f"api:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    while True:
        try:
            await asyncio.sleep(300)  # 每 5 分钟扫描一次；空闲阈值由 kernel_idle_seconds 决定
            await _run_sync_in_thread(_cleanup_kernel_once, owner_id)
        except Exception:
            logger.exception("Kernel 清理异常")


def _recalc_practice_stats_once(owner_id: str):
    from app.services.practice_recalc_job import run_recalc

    with SessionLocal() as db:
        return run_recalc(db, owner_id=owner_id)


async def _practice_stats_recalc():
    """定期重算练习统计/榜单快照（多副本 DB 租约互斥，重算幂等可重入）

    请求路径只读 practice_student_stats 快照，不在请求里聚合全表；间隔见
    settings.practice_stats_recalc_seconds，租约先例见 _cleanup_kernel_once。
    """
    import os
    import socket
    import uuid

    owner_id = f"api:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    while True:
        try:
            interval = max(30.0, float(get_settings().practice_stats_recalc_seconds))
            await asyncio.sleep(interval)
            if get_settings().practice_stats_recalc_enabled:
                await _run_sync_in_thread(_recalc_practice_stats_once, owner_id)
        except Exception:
            logger.exception("练习统计重算异常")


@asynccontextmanager
async def lifespan(app):
    from app.redis_client import build_api_redis
    from app.services import op_metrics

    settings = app.state.settings
    # 有界客户端：0.5s 连接/读取、0 重试、连接池 32（见 app/redis_client.py）
    app.state.redis_client = build_api_redis(settings)
    expiry_task = asyncio.create_task(_expiry_scanner())
    cleanup_task = asyncio.create_task(_kernel_cleanup())
    practice_recalc_task = asyncio.create_task(_practice_stats_recalc())
    # 指标聚合器：每进程唯一 flush 任务，每秒一次；关闭时限内 best-effort
    op_metrics.start_flush_task()
    try:
        yield
    finally:
        expiry_task.cancel()
        cleanup_task.cancel()
        practice_recalc_task.cancel()
        await asyncio.gather(expiry_task, cleanup_task, practice_recalc_task,
                             return_exceptions=True)
        await op_metrics.stop_flush_task()
        close = getattr(app.state.redis_client, "close", None)
        if close is not None:
            close()
        app.state.redis_client = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.state.redis_client = None

    # Request ID 中间件 + 访问指标（TASK-029：状态类别/延迟，低基数路径模板）
    from app.logging_config import set_request_id, setup_logging
    setup_logging(settings.environment, process_name="api", settings=settings)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        from app.logging_config import _request_id_var
        rid = request.headers.get("X-Request-ID", "") or str(uuid.uuid4())[:8]
        token = set_request_id(rid)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            _request_id_var.reset(token)  # 请求结束后重置，避免泄漏到下一个请求
            latency_ms = (time.perf_counter() - start) * 1000
            status_class = f"{status_code // 100}xx"
            # 路径模板（路由匹配后 scope 提供，如 /api/v1/courses/{course_id}）——
            # 绝不记录带资源 id 的原始路径（防高基数与内容泄露）
            path_template = ""
            route = request.scope.get("route")
            if route is not None:
                # FastAPI 0.142 keeps the original APIRoute in scope while
                # its effective context owns the included router prefixes.
                # Both values are registered templates; never use URL.path.
                context = request.scope.get("fastapi", {}).get("effective_route_context")
                path_template = (getattr(context, "path_format", "")
                                 or getattr(route, "path_format", "")
                                 or getattr(route, "path", "") or "")
            log_method = logger.warning if status_code >= 500 else logger.info
            log_method(
                "http_request",
                extra={
                    "method": request.method,
                    "path": path_template or "unmatched",
                    "status_code": status_code,
                    "latency_ms": round(latency_ms, 1),
                },
            )
            _http_metrics(status_class, latency_ms)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError):
        field_errors = []
        for error in exc.errors():
            loc = [str(part) for part in error.get("loc", ()) if part != "body"]
            path = ".".join(loc) or "request"
            message = str(error.get("msg", "请求字段无效")).removeprefix("Value error, ")
            # Pydantic model-level validators report the package item as the
            # location.  Point the admin editor at the actual editable field
            # so its inline error can focus the right input.
            if path.startswith("requested_spec.python_packages.") and path.count(".") == 2:
                if "包名" in message:
                    path += ".name"
                elif "版本" in message:
                    path += ".version"
                elif "import" in message:
                    path += ".import_names"
            elif path.startswith("requested_spec.system_packages.") and path.count(".") == 2:
                path += ".name" if "包名" in message else ".version"
            if path == "python_version":
                code = "PYTHON_VERSION_UNSUPPORTED"
            elif path.startswith("requested_spec.system_packages.") or "系统包" in message:
                code = "APT_PACKAGE_DENIED" if "禁止" in message else "PACKAGE_NAME_INVALID"
            elif path.startswith("requested_spec.python_packages.") or "python 包" in message:
                code = "PACKAGE_NAME_INVALID"
            else:
                # Keep the legacy 422 contract for every non-environment
                # request while exposing structured V2 field details above.
                code = "VALIDATION_ERROR"
            field_errors.append({"path": path, "code": code, "message": message})
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": field_errors[0]["code"] if len(field_errors) == 1 else "VALIDATION_ERROR",
                    "message": "请求参数校验失败",
                    "fields": {"errors": [str(e) for e in exc.errors()]},
                    "field_errors": field_errors,
                    "retryable": False,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": _normalize_detail(exc.detail)},
            headers=exc.headers,
        )

    @app.exception_handler(HTTPException)
    async def fastapi_http_exception_handler(_: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": _normalize_detail(exc.detail)},
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(_: Request, exc: Exception):
        logger.exception("未处理的服务器异常: %s", exc)
        return JSONResponse(status_code=500, content={"detail": {"code": "INTERNAL_ERROR", "message": "服务器内部错误", "fields": {}}})

    @app.get("/api/v1/health/live", tags=["health"])
    def health_live():
        """存活检查——总是返回 ok"""
        return {"status": "ok", "app": settings.app_name}

    @app.get("/api/v1/health/ready", tags=["health"])
    def health_ready(request: Request):
        """就绪检查——验证 MySQL + Redis 可达。

        Redis 承载 Refresh Token、黑名单与队列唤醒，是认证关键依赖；
        任一依赖故障即返回 503，且不回显底层异常详情。
        """
        from app.redis_client import build_api_redis

        ready = True
        details = {}

        # MySQL
        db = None
        try:
            from app.database import SessionLocal
            db = SessionLocal()
            db.execute(select(1))
            details["mysql"] = "ok"
        except Exception:
            ready = False
            details["mysql"] = "unavailable"
        finally:
            if db is not None:
                db.close()

        # Redis
        redis_client = getattr(request.app.state, "redis_client", None)
        owns_redis_client = redis_client is None
        try:
            r = redis_client or build_api_redis(settings)
            r.ping()
            details["redis"] = "ok"
        except Exception:
            ready = False
            details["redis"] = "unavailable"
        finally:
            if owns_redis_client and "r" in locals():
                close = getattr(r, "close", None)
                if close is not None:
                    close()

        status_code = 200 if ready else 503
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status_code,
            content={"status": "ready" if ready else "degraded", "checks": details},
        )

    @app.get("/health", tags=["health"])
    def health():
        return {"status": "ok", "app": settings.app_name}

    @app.get("/api/v1/metrics", tags=["metrics"])
    def metrics(
        current_user=Depends(require_roles("admin")),
        db: Session = Depends(get_db),
        redis_client=Depends(get_redis_client),
    ):
        """内部指标——仅管理员可访问（Nginx 代理后 IP 检查不可靠，改用认证）。

        TASK-029：除队列计数外，增加当前小时 op_metrics 快照、Redis 队列深度、
        最老排队年龄与 DB/Redis 健康；不包含任何提交内容。
        """
        from app.models import CodeGrade, ExamAnswer, Submission
        from app.services.op_metrics import queue_depth, snapshot
        stats = {}
        try:
            db.execute(select(1))
            stats["db_ok"] = True
        except Exception:
            stats["db_ok"] = False
            return {"metrics": stats}
        try:
            stats["assignment_queued"] = db.query(Submission).filter(
                Submission.grading_status == "queued").count()
            stats["assignment_running"] = db.query(Submission).filter(
                Submission.grading_status == "running").count()
            stats["assignment_pending"] = db.query(Submission).filter(
                Submission.grading_status == "pending").count()
            stats["exam_queued"] = db.query(ExamAnswer).filter(
                ExamAnswer.grading_status == "queued").count()
            stats["exam_running"] = db.query(ExamAnswer).filter(
                ExamAnswer.grading_status == "running").count()
            stats["exam_pending"] = db.query(ExamAnswer).filter(
                ExamAnswer.grading_status == "pending").count()
            # 判题队列：DB 事实源（最老 queued 年龄）+ Redis 实际深度
            oldest = db.query(func.min(CodeGrade.queued_at)).filter(
                CodeGrade.status == "queued").scalar()
            if oldest is not None:
                stats["judge_queue_oldest_age_seconds"] = int(
                    (datetime.now(timezone.utc) - oldest).total_seconds()
                )
        except Exception as e:
            stats["db_error"] = str(e)[:100]
            return {"metrics": stats}

        # Redis 侧：队列深度 + 健康 + 当前小时指标快照
        try:
            from app.worker.judge_worker import EXAM_JUDGE_QUEUE

            stats["judge_queue_depth"] = queue_depth(redis_client, settings.judge_queue_name)
            stats["exam_queue_depth"] = queue_depth(redis_client, EXAM_JUDGE_QUEUE)
            stats["ai_queue_depth"] = queue_depth(redis_client, settings.ai_queue_name)
            redis_client.ping()
            stats["redis_ok"] = True
        except Exception:
            stats["redis_ok"] = False
        stats["window"] = "current_hour_utc"
        stats["op_metrics"] = snapshot(redis_client)
        return {"metrics": stats}

    app.include_router(api_router)
    return app


app = create_app()
