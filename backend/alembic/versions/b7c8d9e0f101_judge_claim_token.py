"""判题认领代次：submissions/exam_answers/code_grades 增加 claim_token（+ heartbeat_at）。

目的：让"结果写入"与"仍持有认领"在**同一条条件 UPDATE** 内完成，使恢复机制重新认领后，
失去认领的旧 Worker 无法再覆盖新结果。

- claim_token：每次 queued→running 由 claim_job 现生成的随机值；只增不减，
  任何业务路径（含教师重试重置 attempt_count）都不重置它。
- heartbeat_at：长判题（多测试组）的存活标记，供 stale 恢复区分"崩溃"与"仍在跑"。

可空且无 server default ⇒ MySQL 8 上为仅元数据变更，旧行与旧 Worker 均兼容。
带 inspect 判存，使两阶段 bootstrap 的部分重跑可幂等。

Revision ID: b7c8d9e0f101
Revises: 20261003_0001
Create Date: 2026-10-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7c8d9e0f101"
down_revision: Union[str, None] = "20261003_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = {
    "submissions": ["claim_token", "heartbeat_at"],
    "exam_answers": ["claim_token", "heartbeat_at"],
    "code_grades": ["claim_token"],
}


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table, wanted in _TABLES.items():
        existing = _columns(bind, table)
        with op.batch_alter_table(table) as batch_op:
            if "claim_token" in wanted and "claim_token" not in existing:
                batch_op.add_column(sa.Column("claim_token", sa.String(length=64), nullable=True))
            if "heartbeat_at" in wanted and "heartbeat_at" not in existing:
                batch_op.add_column(
                    sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
                )
        index_name = f"ix_{table}_claim_token"
        if "claim_token" in wanted and index_name not in _indexes(bind, table):
            with op.batch_alter_table(table) as batch_op:
                batch_op.create_index(index_name, ["claim_token"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table, wanted in _TABLES.items():
        if "claim_token" in wanted:
            index_name = f"ix_{table}_claim_token"
            if index_name in _indexes(bind, table):
                with op.batch_alter_table(table) as batch_op:
                    batch_op.drop_index(index_name)
        existing = _columns(bind, table)
        with op.batch_alter_table(table) as batch_op:
            for column in reversed(wanted):
                if column in existing:
                    batch_op.drop_column(column)
