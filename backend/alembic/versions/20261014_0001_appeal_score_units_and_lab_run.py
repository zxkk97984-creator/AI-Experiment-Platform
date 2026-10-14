"""freeze appeal score units and link lab regrades to their run

Revision ID: 20261014_0001
Revises: 20261013_0006
Create Date: 2026-10-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "20261014_0001"
down_revision = "20261013_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grade_appeals") as batch_op:
        batch_op.add_column(sa.Column("score_max", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("regrade_run_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_grade_appeals_regrade_run_id_lab_runs",
            "lab_runs", ["regrade_run_id"], ["id"],
        )
        batch_op.create_index("ix_grade_appeals_regrade_run_id", ["regrade_run_id"])
    with op.batch_alter_table("lab_grade_revisions") as batch_op:
        batch_op.create_unique_constraint("uq_lab_grade_revision_run", ["run_id"])


def downgrade() -> None:
    with op.batch_alter_table("lab_grade_revisions") as batch_op:
        batch_op.drop_constraint("uq_lab_grade_revision_run", type_="unique")
    with op.batch_alter_table("grade_appeals") as batch_op:
        batch_op.drop_index("ix_grade_appeals_regrade_run_id")
        batch_op.drop_constraint("fk_grade_appeals_regrade_run_id_lab_runs", type_="foreignkey")
        batch_op.drop_column("regrade_run_id")
        batch_op.drop_column("score_max")
