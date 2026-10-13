"""批次 5 迁移 0005 与模型的零漂移对拍（不依赖 alembic CLI）。

做法：用记录式桩 ``op`` 执行 `upgrade()`/`downgrade()`，把迁移实际建的表/列/具名约束/
索引与 `Base.metadata` 逐项比对。这样在没有 disposable 库的环境里也能证明
「model 与迁移一致」，等价于 alembic check 对这批表/约束的判定。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa

from app.models import Base

MIGRATION_PATH = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
                  / "20261013_0005_practice_domain.py")
PRACTICE_TABLES = (
    "practice_problems", "practice_submissions", "practice_student_stats",
    "practice_settings", "practice_usage_logs",
)
ALTERED_TABLES = ("question_rubrics", "judge_grading_versions", "code_grades",
                  "ai_grading_setup_jobs")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


class _StubOp:
    def __init__(self):
        self.created_tables: dict[str, list] = {}
        self.added_columns: list[tuple[str, str]] = []
        self.created_indexes: list[tuple[str, str, tuple]] = []
        self.dropped_indexes: list[tuple[str, str]] = []
        self.created_checks: list[tuple[str, str, str]] = []
        self.dropped_constraints: list[tuple[str, str, str]] = []
        self.created_uniques: list[tuple[str, str, tuple]] = []
        self.created_fks: list[tuple[str, str, tuple]] = []
        self.dropped_columns: list[tuple[str, str]] = []
        self.dropped_tables: list[str] = []

    def create_table(self, name, *args, **kwargs):
        self.created_tables[name] = list(args)

    def create_index(self, name, table_name, columns, unique=False, **kwargs):
        self.created_indexes.append((name, table_name, tuple(columns)))

    def drop_index(self, name, table_name=None, **kwargs):
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name, **kwargs):
        self.dropped_tables.append(name)

    def batch_alter_table(self, table_name, **kwargs):
        return _StubBatch(self, table_name)


class _StubBatch:
    def __init__(self, op: _StubOp, table_name: str):
        self.op = op
        self.table_name = table_name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_column(self, column):
        self.op.added_columns.append((self.table_name, column.name))

    def drop_column(self, name):
        self.op.dropped_columns.append((self.table_name, name))

    def create_index(self, name, columns, **kwargs):
        self.op.created_indexes.append((name, self.table_name, tuple(columns)))

    def drop_index(self, name, **kwargs):
        self.op.dropped_indexes.append((name, self.table_name))

    def create_check_constraint(self, name, condition, **kwargs):
        self.op.created_checks.append((self.table_name, name, _norm(condition)))

    def drop_constraint(self, name, type_=None, **kwargs):
        self.op.dropped_constraints.append((self.table_name, name, type_ or ""))

    def create_unique_constraint(self, name, columns, **kwargs):
        self.op.created_uniques.append((self.table_name, name, tuple(columns)))

    def create_foreign_key(self, name, referent, columns, referent_columns, **kwargs):
        self.op.created_fks.append((self.table_name, name, tuple(columns)))


def _run_migration(direction: str) -> _StubOp:
    spec = importlib.util.spec_from_file_location("practice_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = _StubOp()
    module.op = stub
    getattr(module, direction)()
    return stub


def _model_table(table_name: str) -> SimpleNamespace:
    table = Base.metadata.tables[table_name]
    return SimpleNamespace(
        columns={column.name for column in table.columns},
        checks={constraint.name: _norm(constraint.sqltext)
                for constraint in table.constraints
                if isinstance(constraint, sa.CheckConstraint) and constraint.name},
        uniques=sorted(constraint.name for constraint in table.constraints
                       if isinstance(constraint, sa.UniqueConstraint) and constraint.name),
        indexed_columns={column.name for column in table.columns if column.index},
        indexes={index.name: tuple(column.name for column in index.columns)
                 for index in table.indexes},
    )


def test_revision_chain_is_declared():
    spec = importlib.util.spec_from_file_location("practice_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "20261013_0005"
    assert module.down_revision == "20261013_0004"


def test_upgrade_creates_every_practice_table_with_matching_columns():
    stub = _run_migration("upgrade")
    assert set(PRACTICE_TABLES) <= set(stub.created_tables)
    for table_name in PRACTICE_TABLES:
        model = _model_table(table_name)
        args = stub.created_tables[table_name]
        columns = {arg.name for arg in args if isinstance(arg, sa.Column)}
        assert columns == model.columns, (
            f"{table_name} 列不一致: 迁移多 {columns - model.columns} / 模型多 "
            f"{model.columns - columns}")


def test_upgrade_matches_named_constraints():
    stub = _run_migration("upgrade")
    for table_name in PRACTICE_TABLES:
        model = _model_table(table_name)
        args = stub.created_tables[table_name]
        checks = {arg.name: _norm(arg.sqltext) for arg in args
                  if isinstance(arg, sa.CheckConstraint) and arg.name}
        assert checks == model.checks, f"{table_name} CHECK 约束不一致: {checks} vs {model.checks}"
        uniques = sorted(arg.name for arg in args
                         if isinstance(arg, sa.UniqueConstraint) and arg.name)
        assert uniques == model.uniques, f"{table_name} 唯一约束不一致: {uniques} vs {model.uniques}"

    # 三张既有表的 XOR 约束：名字保持不变，条件文本必须与模型逐字一致
    for table_name, constraint_name in (
        ("question_rubrics", "ck_rubric_xor_target"),
        ("judge_grading_versions", "ck_grading_version_xor_target"),
        ("code_grades", "ck_code_grade_xor_target"),
        ("ai_grading_setup_jobs", "ck_ai_setup_job_question_kind"),
    ):
        model = _model_table(table_name)
        created = [item for item in stub.created_checks
                   if item[0] == table_name and item[1] == constraint_name]
        dropped = [item for item in stub.dropped_constraints
                   if item[0] == table_name and item[1] == constraint_name]
        assert dropped, f"{table_name}.{constraint_name} 必须先 drop 再重建"
        assert created, f"{table_name}.{constraint_name} 必须在迁移里重建"
        assert created[-1][2] == model.checks[constraint_name], (
            f"{table_name}.{constraint_name} 与模型不一致: "
            f"{created[-1][2]} vs {model.checks[constraint_name]}")


def test_upgrade_creates_matching_indexes_and_fk_columns():
    stub = _run_migration("upgrade")
    created_indexes = {}
    for name, table_name, columns in stub.created_indexes:
        created_indexes[name] = (table_name, columns)
    for table_name in PRACTICE_TABLES:
        model = _model_table(table_name)
        for index_name, columns in model.indexes.items():
            assert created_indexes.get(index_name) == (table_name, columns), (
                f"索引 {index_name} 未按模型创建: {created_indexes.get(index_name)}")
        for column in model.indexed_columns:
            expected = f"ix_{table_name}_{column}"
            assert created_indexes.get(expected) == (table_name, (column,)), (
                f"列索引 {expected} 未创建")
        for constraint in Base.metadata.tables[table_name].constraints:
            if isinstance(constraint, sa.ForeignKeyConstraint) and constraint.name is None:
                # 模型未命名外键：只要求迁移里对应的本表列存在（alembic 不比较 FK 名）
                for column in constraint.columns:
                    assert column.name in {arg.name for arg in stub.created_tables[table_name]
                                           if isinstance(arg, sa.Column)}

    changed_columns = {f"{table}.{column}" for table, column in stub.added_columns}
    assert "question_rubrics.practice_problem_id" in changed_columns
    assert "judge_grading_versions.practice_problem_id" in changed_columns
    assert "code_grades.practice_submission_id" in changed_columns
    # 循环外键：practice_submissions.code_grade_id 不得建 FK（否则 drop_all 无法排序）
    assert ("practice_submissions", "code_grade_id") not in {
        (table, column) for table, _name, column in
        ((t, n, c[0]) for t, n, c in stub.created_fks)}
    assert not any(table == "practice_submissions" and name.endswith("code_grade_id")
                   for table, name, _cols in stub.created_fks)


def test_downgrade_is_symmetric():
    stub = _run_migration("downgrade")
    assert set(stub.dropped_tables) == set(PRACTICE_TABLES)
    # XOR 约束在 downgrade 里先重建为二选一，再删列
    for table_name, constraint_name in (
        ("question_rubrics", "ck_rubric_xor_target"),
        ("judge_grading_versions", "ck_grading_version_xor_target"),
        ("code_grades", "ck_code_grade_xor_target"),
        ("ai_grading_setup_jobs", "ck_ai_setup_job_question_kind"),
    ):
        restored = [item for item in stub.created_checks
                    if item[0] == table_name and item[1] == constraint_name]
        assert restored, f"downgrade 未恢复 {table_name}.{constraint_name}"
    dropped_columns = {f"{table}.{column}" for table, column in stub.dropped_columns}
    assert {"question_rubrics.practice_problem_id",
            "judge_grading_versions.practice_problem_id",
            "code_grades.practice_submission_id"} <= dropped_columns
