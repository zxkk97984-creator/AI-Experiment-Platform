"""单次成绩异议（批次 2）。

目标用 ``target_kind + target_id`` 多态关联（照 ``ManualScoreAudit`` 先例），
不用 XOR 外键：批次 5 新增自主练习目标时无需再改约束。

「同一 (target, result_revision) 只允许一条非撤回申请」同样用可空唯一键实现：
``dedupe_key`` 在申请有效期内为 ``"{target_kind}:{target_id}:{revision}"``，
撤回时清空该列（NULL 不参与唯一性比较），于是撤回后可以重新申请。
"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin

APPEAL_TARGET_KINDS = ("submission", "exam_answer", "lab_submission", "practice_submission")
APPEAL_OPEN_STATUSES = ("pending", "regrading")
APPEAL_STATUSES = ("pending", "withdrawn", "upheld", "adjusted", "regrading", "rejected")


class GradeAppeal(TimestampMixin, Base):
    __tablename__ = "grade_appeals"
    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('submission', 'exam_answer', 'lab_submission', 'practice_submission')",
            name="ck_grade_appeal_target_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'withdrawn', 'upheld', 'adjusted', 'regrading', 'rejected')",
            name="ck_grade_appeal_status",
        ),
        UniqueConstraint("dedupe_key", name="uq_grade_appeal_dedupe"),
        Index("ix_grade_appeals_target_status", "target_kind", "target_id", "status"),
        Index("ix_grade_appeals_student", "student_id", "created_at"),
        Index("ix_grade_appeals_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # 作业/考试编程题目标的 AI 评分记录；实验与练习目标不使用
    code_grade_id: Mapped[int | None] = mapped_column(
        ForeignKey("code_grades.id"), nullable=True, index=True
    )
    # 申请时的结果修订号快照：重复申请判据与改分后的过时判断都用它
    result_revision_snapshot: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    student_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    handler_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 学生端入口（结果页链接），随目标类型在创建时解析
    route: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 结果对学生可见的时间：异议窗口从这一刻起算
    visible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
