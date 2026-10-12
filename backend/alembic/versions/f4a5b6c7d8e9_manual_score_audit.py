"""人工评分审计表（append-only）：manual_score_audits。

- 每次人工给分新增一行，不修改、不删除；展示用的 result_details.manual_review_history
  只保留最近若干条，事实以本表为准；
- 记录目标（提交/考试答案）、分数、理由、评分者、写入后的结果修订号与前一次分数；
- 结构迁移只加表，不改动任何业务数据。

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-10-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "manual_score_audits"


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, _TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_kind", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("result_revision", sa.Integer(), nullable=False),
        sa.Column("previous_score", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="manual_score"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_manual_score_audits_target", _TABLE, ["target_kind", "target_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, _TABLE):
        op.drop_table(_TABLE)
