"""评分版本（批次三 3B）：judge_grading_versions 表 + 题目/提交/答案绑定字段。

- 新表保存不可变评分版本（草稿→确认→启用→退役），题目与版本严格二选一；
- 绑定字段全部可空：历史题目/提交/答案保持 NULL（不重算、不回填、不绑定最新版本）；
- 考试冻结映射 ``exam_submissions.grading_version_map`` 在开始考试时写入；
- 结构迁移只加表/列/索引，不改变任何业务数据，带 inspect 判存以支持部分重跑。

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-10-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "judge_grading_versions"

_ADDED_COLUMNS: dict[str, list[tuple[str, sa.Column]]] = {
    "judge_questions": [("current_grading_version_id", sa.Column("current_grading_version_id", sa.Integer(), nullable=True))],
    "exam_questions": [("current_grading_version_id", sa.Column("current_grading_version_id", sa.Integer(), nullable=True))],
    "submissions": [("grading_version_id", sa.Column("grading_version_id", sa.Integer(), nullable=True))],
    "exam_answers": [("grading_version_id", sa.Column("grading_version_id", sa.Integer(), nullable=True))],
    "exam_submissions": [("grading_version_map", sa.Column("grading_version_map", sa.JSON(), nullable=True))],
}

_ADDED_INDEXES: dict[str, tuple[str, list[str]]] = {
    "judge_questions": ("ix_judge_questions_current_grading_version_id", ["current_grading_version_id"]),
    "exam_questions": ("ix_exam_questions_current_grading_version_id", ["current_grading_version_id"]),
    "submissions": ("ix_submissions_grading_version_id", ["grading_version_id"]),
    "exam_answers": ("ix_exam_answers_grading_version_id", ["grading_version_id"]),
}


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("judge_question_id", sa.Integer(), sa.ForeignKey("judge_questions.id"), nullable=True),
            sa.Column("exam_question_id", sa.Integer(), sa.ForeignKey("exam_questions.id"), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("execution_mode", sa.String(length=20), nullable=False),
            sa.Column("grading_mode_snapshot", sa.String(length=20), nullable=False),
            sa.Column("entry_function", sa.String(length=120), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("compare_rules", sa.JSON(), nullable=False),
            sa.Column("score_cap_rules", sa.JSON(), nullable=False),
            sa.Column("context_snapshot", sa.JSON(), nullable=False),
            sa.Column("rubric_id", sa.Integer(), sa.ForeignKey("question_rubrics.id"), nullable=True),
            sa.Column("source_kind", sa.String(length=30), nullable=False),
            sa.Column("source_digest", sa.String(length=64), nullable=False),
            sa.Column("spec_digest", sa.String(length=64), nullable=False),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("confirmed_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            # server_default 必须与 TimestampMixin 一致：否则经 alembic 建库后
            # INSERT 会省略这两列并触发 NOT NULL 失败（迁移建表 ≠ create_all 建表）。
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.CheckConstraint(
                "(judge_question_id IS NULL) != (exam_question_id IS NULL)",
                name="ck_grading_version_xor_target",
            ),
            sa.UniqueConstraint("judge_question_id", "version", name="uq_grading_version_judge"),
            sa.UniqueConstraint("exam_question_id", "version", name="uq_grading_version_exam"),
        )
        op.create_index("ix_grading_versions_status", _TABLE, ["status"])
        op.create_index("ix_judge_grading_versions_judge_question_id", _TABLE, ["judge_question_id"])
        op.create_index("ix_judge_grading_versions_exam_question_id", _TABLE, ["exam_question_id"])

    for table, columns in _ADDED_COLUMNS.items():
        existing = _columns(bind, table)
        with op.batch_alter_table(table) as batch_op:
            for name, column in columns:
                if name not in existing:
                    batch_op.add_column(column)

    for table, (index_name, column_names) in _ADDED_INDEXES.items():
        if index_name not in _indexes(bind, table):
            with op.batch_alter_table(table) as batch_op:
                batch_op.create_index(index_name, column_names, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table, (index_name, _) in _ADDED_INDEXES.items():
        if index_name in _indexes(bind, table):
            with op.batch_alter_table(table) as batch_op:
                batch_op.drop_index(index_name)
    for table, columns in _ADDED_COLUMNS.items():
        existing = _columns(bind, table)
        with op.batch_alter_table(table) as batch_op:
            for name, _column in columns:
                if name in existing:
                    batch_op.drop_column(name)
    if _has_table(bind, _TABLE):
        op.drop_table(_TABLE)
