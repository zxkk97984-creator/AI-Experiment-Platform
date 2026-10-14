"""一键 AI 配置任务与草稿试判运行（批次 2）。

任务状态全部落库（MySQL 是唯一事实源），Redis 只做唤醒；父表
``ai_grading_setup_jobs`` 用可空唯一键 ``active_key`` 保证「同一题目的活跃任务唯一」——
MySQL 与 SQLite 都不支持带条件的部分唯一索引，NULL 在两边都不参与唯一性比较，
所以任务进入终态（enabled）时清空该列即可开新一轮配置。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String,
    Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin

SETUP_JOB_STAGES = (
    "queued", "generating_rubric", "generating_test_groups", "draft_ready", "enabled", "failed",
)
TRIAL_RUN_STATUSES = ("queued", "running", "completed", "failed")


class AiGradingSetupJob(TimestampMixin, Base):
    """一键配置任务：生成阶段只产出草稿，教师显式 enable 之前不锁标准、不建版本。"""

    __tablename__ = "ai_grading_setup_jobs"
    __table_args__ = (
        CheckConstraint(
            # practice_problem（批次 5）：管理员在题库编辑页复用本流水线配置 AI 评分
            "question_kind IN ('assignment', 'exam', 'practice_problem')",
            name="ck_ai_setup_job_question_kind",
        ),
        CheckConstraint(
            "stage IN ('queued', 'generating_rubric', 'generating_test_groups',"
            " 'draft_ready', 'enabled', 'failed')",
            name="ck_ai_setup_job_stage",
        ),
        UniqueConstraint("active_key", name="uq_ai_setup_job_active_key"),
        Index("ix_ai_setup_jobs_target", "question_kind", "question_id"),
        Index("ix_ai_setup_jobs_teacher", "teacher_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    question_id: Mapped[int] = mapped_column(Integer, nullable=False)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 活跃任务唯一键 "{question_kind}:{question_id}"；enabled 后清空以允许下一轮配置
    active_key: Mapped[str | None] = mapped_column(String(80), nullable=True)

    requested_reference_solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_teacher_constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_grading_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 教师可只生成评分标准：关闭时草稿阶段直接到 draft_ready，测试组由现有高级面板配置
    generate_test_groups: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )

    draft_rubric_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_rubrics.id"), nullable=True
    )
    draft_test_groups: Mapped[list] = mapped_column(JSON, default=list)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    draft_entry_function: Mapped[str | None] = mapped_column(String(120), nullable=True)
    draft_teacher_constraints: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft_compare_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft_score_cap_rules: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # A retry is durably queued first, then the worker resumes this stage after claiming it.
    resume_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    wake_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 失败阶段：retry 从这里续跑，已生成草稿保留
    failed_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RubricTrialRun(TimestampMixin, Base):
    """草稿试判：只读写本表与草稿 Rubric，绝不创建 Submission/ExamAnswer/CodeGrade。"""

    __tablename__ = "rubric_trial_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_rubric_trial_run_status",
        ),
        UniqueConstraint("job_id", "client_request_id", name="uq_rubric_trial_run_client_request"),
        Index("ix_rubric_trial_runs_job", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("ai_grading_setup_jobs.id"), nullable=False, index=True
    )
    question_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    question_id: Mapped[int] = mapped_column(Integer, nullable=False)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    draft_rubric_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_rubrics.id"), nullable=True
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    # `trial_type=ai_only` is retained for historical A/Q previews. New runs
    # freeze a full deterministic + AI candidate and advance through stages.
    trial_type: Mapped[str] = mapped_column(String(20), default="full", nullable=False)
    stage: Mapped[str] = mapped_column(
        String(30), default="deterministic_queued", nullable=False,
    )
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    candidate_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    candidate_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wake_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    algorithm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    functional_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    robustness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    deterministic_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    preview_score_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    complete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    ai_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
