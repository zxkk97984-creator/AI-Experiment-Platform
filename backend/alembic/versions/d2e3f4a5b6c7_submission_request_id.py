"""作业提交幂等请求键：submissions 增加 client_request_id / request_digest。

- 唯一范围：``(student_id, client_request_id)``——不同学生同键互不影响；
- 历史提交保持 NULL（不重算、不回填）；
- 结构迁移只加列/索引/约束，不改变任何业务数据。

带 inspect 判存，使两阶段 bootstrap 的部分重跑可幂等。

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "submissions"
_UNIQUE_NAME = "uq_submission_student_request"
_INDEX_NAME = "ix_submissions_client_request_id"


def _columns(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def _indexes(bind) -> set[str]:
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(_TABLE)}


def _unique_constraints(bind) -> set[str]:
    names = set()
    for constraint in sa.inspect(bind).get_unique_constraints(_TABLE):
        if constraint.get("name"):
            names.add(constraint["name"])
    return names


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    with op.batch_alter_table(_TABLE) as batch_op:
        if "client_request_id" not in existing:
            batch_op.add_column(sa.Column("client_request_id", sa.String(length=36), nullable=True))
        if "request_digest" not in existing:
            batch_op.add_column(sa.Column("request_digest", sa.String(length=64), nullable=True))
    if _INDEX_NAME not in _indexes(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_index(_INDEX_NAME, ["client_request_id"], unique=False)
    if _UNIQUE_NAME not in _unique_constraints(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_unique_constraint(
                _UNIQUE_NAME, ["student_id", "client_request_id"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _UNIQUE_NAME in _unique_constraints(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_constraint(_UNIQUE_NAME, type_="unique")
    if _INDEX_NAME in _indexes(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_index(_INDEX_NAME)
    existing = _columns(bind)
    with op.batch_alter_table(_TABLE) as batch_op:
        if "request_digest" in existing:
            batch_op.drop_column("request_digest")
        if "client_request_id" in existing:
            batch_op.drop_column("client_request_id")
