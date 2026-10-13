"""异步单元格执行任务——MySQL 是唯一事实源，Redis 只做唤醒。

一次单元格执行 = 一行 kernel_executions；worker 通过 lease_token/lease_until
租约认领并续期，终态转换全部走带 token 的 CAS，旧 worker 无法覆盖新结果。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin

# 非终态（可被认领/恢复）与终态（不再变更）
ACTIVE_EXECUTION_STATUSES = ("queued", "running", "recovering")
TERMINAL_EXECUTION_STATUSES = ("succeeded", "timeout", "failed", "cancelled")


class KernelExecution(TimestampMixin, Base):
    __tablename__ = "kernel_executions"
    __table_args__ = (
        # 同一记录内 client_request_id 幂等：重复点击返回同一行（NULL 不参与去重）
        UniqueConstraint("record_id", "client_request_id", name="uq_kernel_execution_request"),
        Index("ix_kernel_execution_queue", "status", "created_at"),
        Index("ix_kernel_execution_record", "record_id", "status"),
        CheckConstraint(
            "status IN ('queued', 'running', 'recovering', 'succeeded',"
            " 'timeout', 'failed', 'cancelled')",
            name="ck_kernel_execution_status",
        ),
        CheckConstraint("timeout_seconds > 0", name="ck_kernel_execution_timeout"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("experiment_records.id"), index=True)
    cell_id: Mapped[str] = mapped_column(String(80))
    # 代码必须落库：worker 与 API 进程分离，不能依赖请求内存
    code: Mapped[str] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    client_request_id: Mapped[str | None] = mapped_column(String(64))
    # 准入时冻结的超时配置（模板版本发布后不可变，快照保证 worker 判定一致）
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default=text("120"))
    # 准入时的作答/授课上下文，完成时复核，运行期间变更则拒绝落结果
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    outputs: Mapped[list] = mapped_column(JSON, default=list)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer)
    execution_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    # 内核无响应被销毁重建时为 True（内存变量与导入已丢失，必须从头重跑）
    state_lost: Mapped[bool] = mapped_column(Boolean, default=False)
    node_id: Mapped[str | None] = mapped_column(String(80))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
