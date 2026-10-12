"""one-click AI setup jobs, draft trial runs and single grade appeals

Additive: 一键 AI 配置任务（异步生成草稿）与草稿试判运行，以及单次成绩异议
（target_kind + target_id 多态，不用 XOR 外键，批次 5 加练习目标无需再改约束）。

Revision ID: 20261013_0002
Revises: 20261013_0001
Create Date: 2026-10-13 03:20:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '20261013_0002'
down_revision = '20261013_0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_grading_setup_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("question_kind", sa.String(length=20), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("teacher_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        # 活跃任务唯一键：进入 enabled 后清空，允许下一轮一键配置
        sa.Column("active_key", sa.String(length=80), nullable=True),
        sa.Column("requested_reference_solution", sa.Text(), nullable=True),
        sa.Column("requested_teacher_constraints", sa.JSON(), nullable=False),
        sa.Column("requested_grading_mode", sa.String(length=20), nullable=True),
        sa.Column("draft_rubric_id", sa.Integer(), sa.ForeignKey("question_rubrics.id"), nullable=True),
        sa.Column("draft_test_groups", sa.JSON(), nullable=False),
        sa.Column("failed_stage", sa.String(length=30), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # server_default 必须与 TimestampMixin 一致，否则经 alembic 建库后与模型漂移
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("question_kind IN ('assignment', 'exam')",
                           name="ck_ai_setup_job_question_kind"),
        sa.CheckConstraint(
            "stage IN ('queued', 'generating_rubric', 'generating_test_groups',"
            " 'draft_ready', 'enabled', 'failed')",
            name="ck_ai_setup_job_stage",
        ),
        sa.UniqueConstraint("active_key", name="uq_ai_setup_job_active_key"),
    )
    op.create_index("ix_ai_setup_jobs_target", "ai_grading_setup_jobs",
                    ["question_kind", "question_id"], unique=False)
    op.create_index("ix_ai_setup_jobs_teacher", "ai_grading_setup_jobs",
                    ["teacher_id"], unique=False)
    op.create_index("ix_ai_grading_setup_jobs_lease_token", "ai_grading_setup_jobs",
                    ["lease_token"], unique=False)

    op.create_table(
        "rubric_trial_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(),
                  sa.ForeignKey("ai_grading_setup_jobs.id"), nullable=False),
        sa.Column("question_kind", sa.String(length=20), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("teacher_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("draft_rubric_id", sa.Integer(), sa.ForeignKey("question_rubrics.id"), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("algorithm_score", sa.Float(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("preview_score_100", sa.Float(), nullable=True),
        sa.Column("ai_result", sa.JSON(), nullable=True),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')",
                           name="ck_rubric_trial_run_status"),
        sa.UniqueConstraint("job_id", "client_request_id",
                            name="uq_rubric_trial_run_client_request"),
    )
    op.create_index("ix_rubric_trial_runs_job", "rubric_trial_runs",
                    ["job_id", "created_at"], unique=False)
    op.create_index("ix_rubric_trial_runs_job_id", "rubric_trial_runs",
                    ["job_id"], unique=False)
    op.create_index("ix_rubric_trial_runs_lease_token", "rubric_trial_runs",
                    ["lease_token"], unique=False)

    op.create_table(
        "grade_appeals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_kind", sa.String(length=30), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code_grade_id", sa.Integer(), sa.ForeignKey("code_grades.id"), nullable=True),
        sa.Column("result_revision_snapshot", sa.Integer(), nullable=False),
        sa.Column("score_before", sa.Float(), nullable=True),
        sa.Column("score_after", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("student_reply", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("handler_comment", sa.Text(), nullable=True),
        sa.Column("resolved_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("route", sa.String(length=255), nullable=True),
        sa.Column("visible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        # 非撤回申请的唯一键；撤回时清空，允许对同一结果版本重新申请
        sa.Column("dedupe_key", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "target_kind IN ('submission', 'exam_answer', 'lab_submission', 'practice_submission')",
            name="ck_grade_appeal_target_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'withdrawn', 'upheld', 'adjusted', 'regrading', 'rejected')",
            name="ck_grade_appeal_status",
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_grade_appeal_dedupe"),
    )
    op.create_index("ix_grade_appeals_target_status", "grade_appeals",
                    ["target_kind", "target_id", "status"], unique=False)
    op.create_index("ix_grade_appeals_student", "grade_appeals",
                    ["student_id", "created_at"], unique=False)
    op.create_index("ix_grade_appeals_status", "grade_appeals",
                    ["status", "created_at"], unique=False)
    op.create_index("ix_grade_appeals_student_id", "grade_appeals",
                    ["student_id"], unique=False)
    op.create_index("ix_grade_appeals_code_grade_id", "grade_appeals",
                    ["code_grade_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_grade_appeals_code_grade_id", table_name="grade_appeals")
    op.drop_index("ix_grade_appeals_student_id", table_name="grade_appeals")
    op.drop_index("ix_grade_appeals_status", table_name="grade_appeals")
    op.drop_index("ix_grade_appeals_student", table_name="grade_appeals")
    op.drop_index("ix_grade_appeals_target_status", table_name="grade_appeals")
    op.drop_table("grade_appeals")

    op.drop_index("ix_rubric_trial_runs_lease_token", table_name="rubric_trial_runs")
    op.drop_index("ix_rubric_trial_runs_job_id", table_name="rubric_trial_runs")
    op.drop_index("ix_rubric_trial_runs_job", table_name="rubric_trial_runs")
    op.drop_table("rubric_trial_runs")

    op.drop_index("ix_ai_grading_setup_jobs_lease_token", table_name="ai_grading_setup_jobs")
    op.drop_index("ix_ai_setup_jobs_teacher", table_name="ai_grading_setup_jobs")
    op.drop_index("ix_ai_setup_jobs_target", table_name="ai_grading_setup_jobs")
    op.drop_table("ai_grading_setup_jobs")
