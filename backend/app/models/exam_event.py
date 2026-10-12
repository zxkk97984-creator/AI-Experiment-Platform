"""考试秩序与监考事件（批次 3）。

一次考试期间产生的切屏/回到页面/断网重连/换机/写入被拒事件。只记录、只供教师查看，
不自动判定作弊、不自动扣分；IP 与 User-Agent 只保存 HMAC 摘要，永不落明文。
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin


class ExamEvent(TimestampMixin, Base):
    __tablename__ = "exam_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('blur', 'focus', 'reconnect', 'device_switch', 'autosave_rejected', 'warning')",
            name="ck_exam_event_kind",
        ),
        # client_ts 幂等去重：断网重连补报同一条事件不会重复累加监考计数。
        # dedupe_key 为空的行（服务端单次事件）不受此约束限制（SQLite/MySQL 均允许多个 NULL）。
        UniqueConstraint("submission_id", "dedupe_key", name="uq_exam_event_dedupe"),
        Index("ix_exam_events_exam_server_ts", "exam_id", "server_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("exam_submissions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # 客户端事件时刻（补报时原样回传，用于幂等去重）；服务端单次事件为 NULL
    client_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    server_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ua_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<ExamEvent {self.id} {self.kind}>"
