"""practice problem bank, submissions, stats, settings and usage logs

Additive 自主练习域（批次 5 Q8/Q9）：

* 新表 ``practice_problems`` / ``practice_submissions`` / ``practice_student_stats``
  / ``practice_settings`` / ``practice_usage_logs``（与正式作业、考试分表隔离）；
* ``question_rubrics`` / ``judge_grading_versions`` 的 XOR 目标约束扩成三选一
  （加 ``practice_problem_id``）；``code_grades`` 扩成三选一（加
  ``practice_submission_id``）——**约束名全部不变**，仍然是「目标唯一」；
* ``ai_grading_setup_jobs`` 的 ``ck_ai_setup_job_question_kind`` 允许练习题目，
  管理员在题库编辑页复用批次 2 的一键 AI 配置。

Revision ID: 20261013_0005
Revises: 20261013_0004
Create Date: 2026-10-13 05:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '20261013_0005'
down_revision = '20261013_0004'
branch_labels = None
depends_on = None


_RUBRIC_XOR_THREE = (
    "(judge_question_id IS NOT NULL) + (exam_question_id IS NOT NULL)"
    " + (practice_problem_id IS NOT NULL) = 1"
)
_RUBRIC_XOR_TWO = "(judge_question_id IS NULL) != (exam_question_id IS NULL)"
_VERSION_XOR_THREE = (
    "(judge_question_id IS NOT NULL) + (exam_question_id IS NOT NULL)"
    " + (practice_problem_id IS NOT NULL) = 1"
)
_VERSION_XOR_TWO = "(judge_question_id IS NULL) != (exam_question_id IS NULL)"
_CODE_GRADE_XOR_THREE = (
    "(submission_id IS NOT NULL) + (exam_answer_id IS NOT NULL)"
    " + (practice_submission_id IS NOT NULL) = 1"
)
_CODE_GRADE_XOR_TWO = "(submission_id IS NULL) != (exam_answer_id IS NULL)"
_SETUP_JOB_KINDS_WITH_PRACTICE = "question_kind IN ('assignment', 'exam', 'practice_problem')"
_SETUP_JOB_KINDS_WITHOUT_PRACTICE = "question_kind IN ('assignment', 'exam')"


def upgrade() -> None:
    op.create_table(
        "practice_problems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("difficulty", sa.String(length=10), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("starter_code", sa.Text(), nullable=True),
        sa.Column("public_cases", sa.JSON(), nullable=False),
        sa.Column("hidden_tests", sa.JSON(), nullable=False),
        sa.Column("test_groups", sa.JSON(), nullable=False),
        sa.Column("time_limit_ms", sa.Integer(), nullable=False),
        sa.Column("memory_limit_kb", sa.Integer(), nullable=False),
        sa.Column("grading_mode", sa.String(length=20), nullable=False),
        sa.Column("teacher_constraints", sa.JSON(), nullable=False),
        sa.Column("reference_solution", sa.Text(), nullable=True),
        sa.Column("score_cap_rules", sa.JSON(), nullable=False),
        sa.Column("function_name", sa.String(length=120), nullable=False),
        # 用整数而非外键：评分版本反向引用题目，避免循环外键（同 JudgeQuestion 先例）
        sa.Column("current_grading_version_id", sa.Integer(), nullable=True),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_judge_question_id", sa.Integer(),
                  sa.ForeignKey("judge_questions.id"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_attempts", sa.Integer(), nullable=False),
        sa.Column("distinct_solvers", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("difficulty IN ('easy', 'medium', 'hard')",
                           name="ck_practice_problem_difficulty"),
        sa.CheckConstraint("status IN ('draft', 'published', 'archived')",
                           name="ck_practice_problem_status"),
        sa.CheckConstraint("grading_mode IN ('deterministic', 'ai')",
                           name="ck_practice_problem_grading_mode"),
        sa.CheckConstraint(
            "source_kind IN ('admin_made', 'ai_generated', 'from_assignment')",
            name="ck_practice_problem_source_kind"),
    )
    op.create_index("ix_practice_problems_status", "practice_problems",
                    ["status", "created_at"], unique=False)
    op.create_index("ix_practice_problems_current_grading_version_id", "practice_problems",
                    ["current_grading_version_id"], unique=False)
    op.create_index("ix_practice_problems_created_by_id", "practice_problems",
                    ["created_by_id"], unique=False)

    op.create_table(
        "practice_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("problem_id", sa.Integer(),
                  sa.ForeignKey("practice_problems.id"), nullable=False),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("grading_mode", sa.String(length=20), nullable=False),
        sa.Column("grading_version_id", sa.Integer(),
                  sa.ForeignKey("judge_grading_versions.id"), nullable=True),
        # 不加外键：code_grades.practice_submission_id 已反向指向本表，
        # 再建 FK 会形成循环外键依赖（drop_all 无法排序）；权威关联在 code_grades 侧。
        sa.Column("code_grade_id", sa.Integer(), nullable=True),
        sa.Column("grading_status", sa.String(length=20), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("result_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("max_score", sa.Float(), nullable=False),
        sa.Column("deterministic_score", sa.Float(), nullable=True),
        sa.Column("functional_score", sa.Float(), nullable=True),
        sa.Column("algorithm_score", sa.Float(), nullable=True),
        sa.Column("robustness_score", sa.Float(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("tests_passed", sa.Integer(), nullable=True),
        sa.Column("tests_total", sa.Integer(), nullable=True),
        sa.Column("result_details", sa.JSON(), nullable=False),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("execution_time_ms", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("counted_for_rank", sa.Boolean(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("grading_mode IN ('deterministic', 'ai')",
                           name="ck_practice_submission_grading_mode"),
        sa.CheckConstraint(
            "grading_status IN ('queued', 'running', 'completed', 'failed', 'review_required')",
            name="ck_practice_submission_status"),
        sa.UniqueConstraint("problem_id", "student_id", "client_request_id",
                            name="uq_practice_submission_idempotency"),
    )
    op.create_index("ix_practice_submissions_problem_id", "practice_submissions",
                    ["problem_id"], unique=False)
    op.create_index("ix_practice_submissions_student_id", "practice_submissions",
                    ["student_id"], unique=False)
    op.create_index("ix_practice_submissions_grading_version_id", "practice_submissions",
                    ["grading_version_id"], unique=False)
    op.create_index("ix_practice_submissions_code_grade_id", "practice_submissions",
                    ["code_grade_id"], unique=False)
    op.create_index("ix_practice_submissions_claim_token", "practice_submissions",
                    ["claim_token"], unique=False)
    op.create_index("ix_practice_submissions_student_created", "practice_submissions",
                    ["student_id", "created_at"], unique=False)
    op.create_index("ix_practice_submissions_problem_student_score", "practice_submissions",
                    ["problem_id", "student_id", "score"], unique=False)
    op.create_index("ix_practice_submissions_problem_version", "practice_submissions",
                    ["problem_id", "grading_version_id"], unique=False)
    op.create_index("ix_practice_submissions_status", "practice_submissions",
                    ["grading_status", "updated_at"], unique=False)

    op.create_table(
        "practice_student_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope_kind", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("board_kind", sa.String(length=20), nullable=False),
        sa.Column("solved_count", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("avg_score", sa.Float(), nullable=True),
        sa.Column("accuracy", sa.Float(), nullable=True),
        sa.Column("streak_days", sa.Integer(), nullable=False),
        sa.Column("last_active_date", sa.Date(), nullable=True),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("scope_kind IN ('offering', 'platform')",
                           name="ck_practice_stat_scope_kind"),
        sa.CheckConstraint("board_kind IN ('deterministic', 'ai')",
                           name="ck_practice_stat_board_kind"),
        sa.UniqueConstraint("student_id", "scope_kind", "scope_id", "board_kind",
                            name="uq_practice_stat_scope_board"),
    )
    op.create_index("ix_practice_student_stats_student_id", "practice_student_stats",
                    ["student_id"], unique=False)
    op.create_index("ix_practice_student_stats_board", "practice_student_stats",
                    ["scope_kind", "scope_id", "board_kind", "total_score"], unique=False)

    op.create_table(
        "practice_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("grading_mode", sa.String(length=20), nullable=False),
        sa.Column("daily_submission_cap_per_student", sa.Integer(), nullable=False),
        sa.Column("daily_ai_budget_tokens", sa.Integer(), nullable=False),
        sa.Column("daily_ai_budget_amount", sa.Float(), nullable=False),
        sa.Column("ai_amount_per_1k_tokens", sa.Float(), nullable=False),
        sa.Column("max_concurrent_practice_jobs", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_practice_settings_singleton"),
        sa.CheckConstraint("grading_mode IN ('deterministic', 'ai')",
                           name="ck_practice_settings_grading_mode"),
    )

    op.create_table(
        "practice_usage_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=30), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("date", "scope", "student_id", "metric",
                            name="uq_practice_usage_daily"),
    )
    op.create_index("ix_practice_usage_logs_date_metric", "practice_usage_logs",
                    ["date", "metric"], unique=False)

    # ── XOR 目标三选一（加练习目标；约束名不变） ──────────────
    with op.batch_alter_table("question_rubrics") as batch:
        batch.add_column(sa.Column("practice_problem_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_question_rubrics_practice_problem_id",
                                 "practice_problems", ["practice_problem_id"], ["id"])
        batch.create_index("ix_question_rubrics_practice_problem_id", ["practice_problem_id"])
        batch.drop_constraint("ck_rubric_xor_target", type_="check")
        batch.create_check_constraint("ck_rubric_xor_target", _RUBRIC_XOR_THREE)
        batch.create_unique_constraint("uq_rubric_practice_version",
                                       ["practice_problem_id", "version"])

    with op.batch_alter_table("judge_grading_versions") as batch:
        batch.add_column(sa.Column("practice_problem_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_judge_grading_versions_practice_problem_id",
                                 "practice_problems", ["practice_problem_id"], ["id"])
        batch.create_index("ix_judge_grading_versions_practice_problem_id",
                           ["practice_problem_id"])
        batch.drop_constraint("ck_grading_version_xor_target", type_="check")
        batch.create_check_constraint("ck_grading_version_xor_target", _VERSION_XOR_THREE)
        batch.create_unique_constraint("uq_grading_version_practice",
                                       ["practice_problem_id", "version"])

    with op.batch_alter_table("code_grades") as batch:
        batch.add_column(sa.Column("practice_submission_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_code_grades_practice_submission_id",
                                 "practice_submissions", ["practice_submission_id"], ["id"])
        batch.create_unique_constraint("uq_code_grades_practice_submission_id",
                                       ["practice_submission_id"])
        batch.drop_constraint("ck_code_grade_xor_target", type_="check")
        batch.create_check_constraint("ck_code_grade_xor_target", _CODE_GRADE_XOR_THREE)

    # 一键 AI 配置：练习题目复用批次 2 的生成/试判/启用流水线
    with op.batch_alter_table("ai_grading_setup_jobs") as batch:
        batch.drop_constraint("ck_ai_setup_job_question_kind", type_="check")
        batch.create_check_constraint("ck_ai_setup_job_question_kind",
                                      _SETUP_JOB_KINDS_WITH_PRACTICE)


def downgrade() -> None:
    with op.batch_alter_table("ai_grading_setup_jobs") as batch:
        batch.drop_constraint("ck_ai_setup_job_question_kind", type_="check")
        batch.create_check_constraint("ck_ai_setup_job_question_kind",
                                      _SETUP_JOB_KINDS_WITHOUT_PRACTICE)

    with op.batch_alter_table("code_grades") as batch:
        batch.drop_constraint("ck_code_grade_xor_target", type_="check")
        batch.create_check_constraint("ck_code_grade_xor_target", _CODE_GRADE_XOR_TWO)
        batch.drop_constraint("uq_code_grades_practice_submission_id", type_="unique")
        batch.drop_constraint("fk_code_grades_practice_submission_id", type_="foreignkey")
        batch.drop_column("practice_submission_id")

    with op.batch_alter_table("judge_grading_versions") as batch:
        batch.drop_constraint("ck_grading_version_xor_target", type_="check")
        batch.create_check_constraint("ck_grading_version_xor_target", _VERSION_XOR_TWO)
        batch.drop_constraint("uq_grading_version_practice", type_="unique")
        batch.drop_index("ix_judge_grading_versions_practice_problem_id")
        batch.drop_constraint("fk_judge_grading_versions_practice_problem_id", type_="foreignkey")
        batch.drop_column("practice_problem_id")

    with op.batch_alter_table("question_rubrics") as batch:
        batch.drop_constraint("ck_rubric_xor_target", type_="check")
        batch.create_check_constraint("ck_rubric_xor_target", _RUBRIC_XOR_TWO)
        batch.drop_constraint("uq_rubric_practice_version", type_="unique")
        batch.drop_index("ix_question_rubrics_practice_problem_id")
        batch.drop_constraint("fk_question_rubrics_practice_problem_id", type_="foreignkey")
        batch.drop_column("practice_problem_id")

    op.drop_index("ix_practice_usage_logs_date_metric", table_name="practice_usage_logs")
    op.drop_table("practice_usage_logs")
    op.drop_table("practice_settings")
    op.drop_index("ix_practice_student_stats_board", table_name="practice_student_stats")
    op.drop_index("ix_practice_student_stats_student_id", table_name="practice_student_stats")
    op.drop_table("practice_student_stats")
    for index_name in (
        "ix_practice_submissions_status",
        "ix_practice_submissions_problem_version",
        "ix_practice_submissions_problem_student_score",
        "ix_practice_submissions_student_created",
        "ix_practice_submissions_claim_token",
        "ix_practice_submissions_code_grade_id",
        "ix_practice_submissions_grading_version_id",
        "ix_practice_submissions_student_id",
        "ix_practice_submissions_problem_id",
    ):
        op.drop_index(index_name, table_name="practice_submissions")
    op.drop_table("practice_submissions")
    op.drop_index("ix_practice_problems_created_by_id", table_name="practice_problems")
    op.drop_index("ix_practice_problems_current_grading_version_id", table_name="practice_problems")
    op.drop_index("ix_practice_problems_status", table_name="practice_problems")
    op.drop_table("practice_problems")
