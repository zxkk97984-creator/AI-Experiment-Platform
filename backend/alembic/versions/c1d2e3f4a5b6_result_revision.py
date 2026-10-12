"""结果修订号：submissions/exam_answers/code_grades 增加 result_revision。

目的：任何产生或改变结果的写入（判题终态、AI 终态、人工改分、受控重评）都必须在
**同一事务**内原子推进一个只增不减的修订号；人工评分与旧客户端据此携带
``expected_result_revision`` 拒绝过时写入，而不必依赖内容比对或时间戳。

非空 + server_default="0" ⇒ 旧行自动取 0，MySQL 8 上为就地默认值变更，
历史成绩与评分证据保持不变；不做任何批量重算。

带 inspect 判存，使两阶段 bootstrap 的部分重跑可幂等。

Revision ID: c1d2e3f4a5b6
Revises: b7c8d9e0f101
Create Date: 2026-10-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b7c8d9e0f101"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("submissions", "exam_answers", "code_grades")


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        if "result_revision" in _columns(bind, table):
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "result_revision",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        if "result_revision" not in _columns(bind, table):
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("result_revision")
