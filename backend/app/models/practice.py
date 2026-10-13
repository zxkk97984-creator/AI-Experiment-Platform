"""自主练习域（批次 5 Q8/Q9）：公共题库、练习提交、个人统计、平台设置与用量。

与正式作业/考试**分表隔离**：练习不使用 ``Assignment``/``Submission``，
所以不受 ``services/teaching_query_policy.py`` 的教师行级过滤影响（跨班、学生自主）。
练习成绩、排名与徽章一律不进课程总评。

计分口径（与 ``JudgeGradingVersion`` 的目标列扩展呼应）：

* 每题只累计**当前评分版本**下的历史最高分；改题生成新版本后旧成绩留在历史里，
  不与当前版本混排（排名查询按 ``(practice_problem_id, grading_mode)`` 取最新版本）。
* ``PracticeSubmission.grading_mode`` 与 ``grading_version_id`` 都是**提交时刻快照**：
  管理员切换全局判题方式只影响新提交，历史提交保留原模式与结果。
"""
from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models import TimestampMixin

PRACTICE_DIFFICULTIES = ("easy", "medium", "hard")
PRACTICE_STATUSES = ("draft", "published", "archived")
PRACTICE_GRADING_MODES = ("deterministic", "ai")
PRACTICE_SOURCE_KINDS = ("admin_made", "ai_generated", "from_assignment")
# queued/running 之外都是终态；review_required 表示 AI 未完成或平台配置问题，
# 此时分数只保留已完成的确定性部分，绝不写成 0 分、也不当成完整 AI 成绩。
PRACTICE_SUBMISSION_STATUSES = (
    "queued", "running", "completed", "failed", "review_required",
)
PRACTICE_SCOPE_KINDS = ("offering", "platform")
PRACTICE_BOARD_KINDS = ("deterministic", "ai")
PRACTICE_METRICS = ("submissions", "ai_tokens", "ai_amount")


class PracticeProblem(TimestampMixin, Base):
    """平台级公共题库题目（管理员维护；教师只读排行榜）。"""

    __tablename__ = "practice_problems"
    __table_args__ = (
        CheckConstraint(
            "difficulty IN ('easy', 'medium', 'hard')",
            name="ck_practice_problem_difficulty",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_practice_problem_status",
        ),
        CheckConstraint(
            "grading_mode IN ('deterministic', 'ai')",
            name="ck_practice_problem_grading_mode",
        ),
        CheckConstraint(
            "source_kind IN ('admin_made', 'ai_generated', 'from_assignment')",
            name="ck_practice_problem_source_kind",
        ),
        Index("ix_practice_problems_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    difficulty: Mapped[str] = mapped_column(String(10), default="medium")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    prompt: Mapped[str] = mapped_column(Text, default="")
    starter_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 学生可见示例用例；判题用的是 hidden_tests（形状一致：id/input/expected/compare）
    public_cases: Mapped[list] = mapped_column(JSON, default=list)
    hidden_tests: Mapped[list] = mapped_column(JSON, default=list)
    # 一键 AI 配置生成的测试组（批次 2 的组件直接复用）
    test_groups: Mapped[list] = mapped_column(JSON, default=list)
    time_limit_ms: Mapped[int] = mapped_column(Integer, default=10000)
    memory_limit_kb: Mapped[int] = mapped_column(Integer, default=262144)
    # 题目的**创作口径**：deterministic（普通判题）/ ai（AI 综合判题）。
    # 全局开关 practice_settings.grading_mode 打开 AI 时，只有 ai 口径且评分标准
    # 已就绪的题目才会让新提交走 AI；未准备完成的题目在预检里被列出。
    grading_mode: Mapped[str] = mapped_column(String(20), default="deterministic")
    teacher_constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    reference_solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_cap_rules: Mapped[list] = mapped_column(JSON, default=list)
    function_name: Mapped[str] = mapped_column(String(120), default="solution")
    # 指向 judge_grading_versions.id；用整数而非外键，避免与评分版本表（反向引用题目）
    # 形成循环外键（与 JudgeQuestion.current_grading_version_id 同一做法）。
    current_grading_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_kind: Mapped[str] = mapped_column(String(20), default="admin_made")
    source_judge_question_id: Mapped[int | None] = mapped_column(
        ForeignKey("judge_questions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="draft")
    total_attempts: Mapped[int] = mapped_column(Integer, default=0)
    distinct_solvers: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PracticeSubmission(TimestampMixin, Base):
    """练习提交——与正式提交分表；判题队列独立，绝不挤占 judge:* 容量。"""

    __tablename__ = "practice_submissions"
    __table_args__ = (
        CheckConstraint(
            "grading_mode IN ('deterministic', 'ai')",
            name="ck_practice_submission_grading_mode",
        ),
        CheckConstraint(
            "grading_status IN ('queued', 'running', 'completed', 'failed', 'review_required')",
            name="ck_practice_submission_status",
        ),
        # 幂等：同一学生同一题的同一 client_request_id 只建一条提交
        UniqueConstraint(
            "problem_id", "student_id", "client_request_id",
            name="uq_practice_submission_idempotency",
        ),
        Index("ix_practice_submissions_student_created", "student_id", "created_at"),
        Index("ix_practice_submissions_problem_student_score", "problem_id", "student_id", "score"),
        Index("ix_practice_submissions_problem_version", "problem_id", "grading_version_id"),
        Index("ix_practice_submissions_status", "grading_status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("practice_problems.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code: Mapped[str] = mapped_column(Text)
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 代码摘要：同一幂等键但代码变了要显式报冲突，不能静默返回旧结果
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # ── 提交时刻快照：切换判题方式/改题都不影响这条提交 ──
    grading_mode: Mapped[str] = mapped_column(String(20), default="deterministic")
    grading_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("judge_grading_versions.id"), nullable=True, index=True
    )
    # 不加外键：``code_grades.practice_submission_id`` 已经反向指向本表，
    # 这里再建 FK 会形成循环外键依赖，导致 Base.metadata.drop_all 无法排序
    # （测试 teardown 全库崩）。权威关联以 code_grades.practice_submission_id 为准。
    code_grade_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # ── 判题状态与结果（认领栅栏：claim_token 用于条件 UPDATE，绝不裸赋值写分） ──
    grading_status: Mapped[str] = mapped_column(String(20), default="queued")
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    result_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score: Mapped[float] = mapped_column(Float, default=100.0)
    # 确定性判题已完成的分数（0–100）：AI 未完成时对外展示的就是它
    deterministic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    functional_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    algorithm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    robustness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tests_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_details: Mapped[dict] = mapped_column(JSON, default=dict)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    counted_for_rank: Mapped[bool] = mapped_column(Boolean, default=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PracticeStudentStat(TimestampMixin, Base):
    """个人统计/榜单聚合快照（批次 6 由 scheduler_lease 保护的定时任务重算）。"""

    __tablename__ = "practice_student_stats"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('offering', 'platform')",
            name="ck_practice_stat_scope_kind",
        ),
        CheckConstraint(
            "board_kind IN ('deterministic', 'ai')",
            name="ck_practice_stat_board_kind",
        ),
        UniqueConstraint(
            "student_id", "scope_kind", "scope_id", "board_kind",
            name="uq_practice_stat_scope_board",
        ),
        Index("ix_practice_student_stats_board", "scope_kind", "scope_id", "board_kind", "total_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    scope_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # platform 榜用 0；班内榜用 offering_id
    scope_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    board_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    solved_count: Mapped[int] = mapped_column(Integer, default=0)
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    avg_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_active_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PracticeSettings(TimestampMixin, Base):
    """单行平台设置：全局判题方式、额度与并发上限（管理员维护）。"""

    __tablename__ = "practice_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_practice_settings_singleton"),
        CheckConstraint(
            "grading_mode IN ('deterministic', 'ai')",
            name="ck_practice_settings_grading_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    grading_mode: Mapped[str] = mapped_column(String(20), default="deterministic")
    # 每生每天提交次数上限（0 = 不限制）
    daily_submission_cap_per_student: Mapped[int] = mapped_column(Integer, default=50)
    # 全平台每天 AI token 预算（0 = 不限制）；超预算的新提交直接拒绝（QUOTA_EXHAUSTED）
    daily_ai_budget_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # 全平台每天 AI 计费额度；按 token 折算（ai_amount_per_1k_tokens），0 = 不限制
    daily_ai_budget_amount: Mapped[float] = mapped_column(Float, default=0.0)
    ai_amount_per_1k_tokens: Mapped[float] = mapped_column(Float, default=0.01)
    # 练习 worker 并发上限：独立队列 + 受限并发，保留正式判题容量
    max_concurrent_practice_jobs: Mapped[int] = mapped_column(Integer, default=2)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class PracticeUsageLog(TimestampMixin, Base):
    """按天用量台账（唯一 (date, scope, student_id, metric)）：额度以库为准，Redis 只做计数。"""

    __tablename__ = "practice_usage_logs"
    __table_args__ = (
        UniqueConstraint(
            "date", "scope", "student_id", "metric",
            name="uq_practice_usage_daily",
        ),
        Index("ix_practice_usage_logs_date_metric", "date", "metric"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    # platform / student
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    # 平台级用量用 0
    student_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metric: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
