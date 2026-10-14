"""editable AI setup drafts and immutable full trial snapshots

Revision ID: 20261014_0002
Revises: 20261014_0001
Create Date: 2026-10-14 00:00:01.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20261014_0002"
down_revision = "20261014_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ai_grading_setup_jobs") as batch_op:
        batch_op.add_column(sa.Column("draft_revision", sa.Integer(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("draft_entry_function", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("draft_teacher_constraints", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("draft_compare_rules", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("draft_score_cap_rules", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("resume_stage", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("wake_published_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("rubric_trial_runs") as batch_op:
        batch_op.add_column(sa.Column("trial_type", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("stage", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("draft_revision", sa.Integer(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("candidate_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("candidate_digest", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("wake_published_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("functional_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("robustness_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("deterministic_details", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("total_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("score_max", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("complete", sa.Boolean(), nullable=False,
                                      server_default=sa.text("0")))

    op.execute(sa.text("""
        UPDATE rubric_trial_runs
        SET trial_type = 'ai_only',
            stage = CASE status
                WHEN 'queued' THEN 'ai_queued'
                WHEN 'running' THEN 'ai_running'
                WHEN 'completed' THEN 'completed'
                ELSE 'failed'
            END
        WHERE trial_type IS NULL OR stage IS NULL
    """))
    with op.batch_alter_table("rubric_trial_runs") as batch_op:
        batch_op.alter_column("trial_type", existing_type=sa.String(length=20), nullable=False)
        batch_op.alter_column("stage", existing_type=sa.String(length=30), nullable=False)
    op.execute(sa.text("""
        UPDATE ai_grading_setup_jobs
        SET draft_revision = 1
        WHERE draft_rubric_id IS NOT NULL AND stage IN ('draft_ready', 'enabled')
    """))


def downgrade() -> None:
    with op.batch_alter_table("rubric_trial_runs") as batch_op:
        batch_op.drop_column("complete")
        batch_op.drop_column("score_max")
        batch_op.drop_column("total_score")
        batch_op.drop_column("deterministic_details")
        batch_op.drop_column("robustness_score")
        batch_op.drop_column("functional_score")
        batch_op.drop_column("candidate_digest")
        batch_op.drop_column("candidate_snapshot")
        batch_op.drop_column("draft_revision")
        batch_op.drop_column("stage")
        batch_op.drop_column("trial_type")
        batch_op.drop_column("wake_published_at")
    with op.batch_alter_table("ai_grading_setup_jobs") as batch_op:
        batch_op.drop_column("resume_stage")
        batch_op.drop_column("wake_published_at")
        batch_op.drop_column("draft_score_cap_rules")
        batch_op.drop_column("draft_compare_rules")
        batch_op.drop_column("draft_teacher_constraints")
        batch_op.drop_column("draft_entry_function")
        batch_op.drop_column("draft_revision")
