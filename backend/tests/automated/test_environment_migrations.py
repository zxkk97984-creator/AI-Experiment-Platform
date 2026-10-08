"""环境控制面迁移测试（Phase 1：迁移 A）+ 业务绑定迁移测试（Phase 3：迁移 B）

- 单迁移在隔离 SQLite 上验证：建五张表、列类型/约束、downgrade 逆序删除
- 迁移 B：业务绑定列（nullable 加列 → 存量回填 basic → 改 NOT NULL → FK/索引），
  以及无 basic 可用版本时主动失败（fail-closed）
- revision 链：b4c5d6e7f890 → down_revision=a3b4c5d6e789；c5d6e7f8a901 → down_revision=b4c5d6e7f890
- 全链验证：隔离 SQLite 文件库跑 alembic 分段 upgrade（迁移 A 后插入 basic 种子）→
  current 到新 head → downgrade 可逆
- 绝不触碰开发库（DAI_DATABASE_URL 指向临时文件）
"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.no_auto_env_seed

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

BACKEND_DIR = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    BACKEND_DIR / "alembic" / "versions" / "b4c5d6e7f890_add_environment_control_plane.py"
)
MIGRATION_B_PATH = (
    BACKEND_DIR / "alembic" / "versions" / "c5d6e7f8a901_bind_environment_versions.py"
)
MIGRATION_FIX_PATH = (
    BACKEND_DIR / "alembic" / "versions" / "d6e7f8a9b012_make_judge_questions_env_nullable.py"
)
PREV_REVISION = "a3b4c5d6e789"
NEW_REVISION = "b4c5d6e7f890"
REVISION_A = "b4c5d6e7f890"
REVISION_B = "c5d6e7f8a901"
REVISION_FIX = "d6e7f8a9b012"


def _derive_current_head() -> str:
    """从 alembic 脚本目录推导当前 head（随新增迁移自动前移，避免硬编码漂移）。

    显式以绝对路径覆盖 script_location：从仓库根目录收集测试时 cwd 不是 backend，
    相对路径会指向不存在的目录。
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"迁移链出现多个 head: {heads}"
    return heads[0]


CURRENT_HEAD = _derive_current_head()

CONTROL_TABLES = [
    "package_catalog",
    "environment_profiles",
    "environment_versions",
    "profile_version_packages",
    "environment_build_jobs",
]

# 迁移 B 绑定的业务表与其绑定列（plan 4.6）
BINDING_TABLES = {
    "assignments": ("environment_version_id", "import_policy_mode", "allowed_imports"),
    "judge_questions": ("environment_version_id", "import_policy_mode", "allowed_imports"),
    "submissions": ("environment_version_id", "import_policy_mode_snapshot", "allowed_imports_snapshot"),
    "notebook_templates": ("draft_environment_version_id", "draft_import_policy_mode", "draft_allowed_imports"),
    "notebook_template_versions": ("environment_version_id", "import_policy_mode", "allowed_imports"),
    "experiment_records": ("environment_version_id",),
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("env_control_plane_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_schema(engine) -> None:
    """只创建 upgrade 需要引用的前置表（users），不依赖 ORM 模型。"""
    meta = sa.MetaData()
    sa.Table(
        "users",
        meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(80)),
        sa.Column("role", sa.String(30)),
    )
    meta.create_all(engine)


@pytest.fixture()
def engine():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    try:
        yield engine
    finally:
        engine.dispose()


def _run_upgrade(engine, migration):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()


def _run_downgrade(engine, migration):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()


# ═══════════════════════════════════════════════════════════════
# revision 链
# ═══════════════════════════════════════════════════════════════

def test_revision_chain_points_to_current_head():
    migration = _load_migration()
    assert migration.revision == NEW_REVISION
    assert migration.down_revision == PREV_REVISION


def test_no_branch_labels():
    migration = _load_migration()
    assert migration.branch_labels is None
    assert migration.depends_on is None


# ═══════════════════════════════════════════════════════════════
# upgrade 建表
# ═══════════════════════════════════════════════════════════════

def test_upgrade_creates_all_control_tables(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    inspector = sa.inspect(engine)
    for t in CONTROL_TABLES:
        assert t in inspector.get_table_names(), f"缺少表 {t}"


def test_package_catalog_columns_and_constraints(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    inspector = sa.inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("package_catalog")}
    assert cols["id"]["nullable"] is False
    assert cols["normalized_name"]["type"].length == 128
    assert cols["normalized_name"]["nullable"] is False
    assert cols["pip_name"]["type"].length == 128
    assert cols["locked_version"]["type"].length == 64
    assert cols["locked_version"]["nullable"] is False
    assert cols["source_key"]["type"].length == 32
    assert cols["status"]["type"].length == 16
    assert cols["supersedes_id"]["nullable"] is True
    assert cols["created_by_id"]["nullable"] is True

    uniques = inspector.get_unique_constraints("package_catalog")
    assert any(set(u["column_names"]) == {"normalized_name", "locked_version", "source_key"} for u in uniques), \
        "缺少 (normalized_name, locked_version, source_key) 唯一约束"


def test_environment_profiles_columns(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    inspector = sa.inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("environment_profiles")}
    assert cols["slug"]["type"].length == 80
    assert cols["slug"]["nullable"] is False
    assert cols["display_name"]["type"].length == 120
    assert cols["description"]["nullable"] is True
    assert cols["status"]["type"].length == 16
    uniques = inspector.get_unique_constraints("environment_profiles")
    assert any(set(u["column_names"]) == {"slug"} for u in uniques)


def test_environment_versions_columns_and_constraints(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    inspector = sa.inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("environment_versions")}
    assert cols["version_number"]["nullable"] is False
    assert cols["status"]["type"].length == 20
    assert cols["base_image_ref"]["type"].length == 255
    assert cols["base_image_ref"]["nullable"] is False
    assert cols["image_tag"]["type"].length == 255
    assert cols["image_digest"]["type"].length == 255
    assert cols["manifest_sha256"]["type"].length == 64
    assert cols["manifest_sha256"]["nullable"] is False
    assert cols["minimum_memory_mb"]["nullable"] is False
    assert cols["python_version"]["nullable"] is True
    assert cols["source_version_id"]["nullable"] is True

    uniques = inspector.get_unique_constraints("environment_versions")
    names = {frozenset(u["column_names"]) for u in uniques}
    assert frozenset({"profile_id", "version_number"}) in names
    assert frozenset({"image_tag"}) in names
    assert frozenset({"image_digest"}) in names


def test_build_job_columns_and_index(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    inspector = sa.inspect(engine)

    cols = {c["name"]: c for c in inspector.get_columns("environment_build_jobs")}
    assert cols["status"]["type"].length == 20
    assert cols["attempt_number"]["nullable"] is False
    assert cols["worker_id"]["type"].length == 160
    assert cols["error_message"]["type"].length == 500
    assert cols["retry_of_id"]["nullable"] is True

    indexes = inspector.get_indexes("environment_build_jobs")
    assert any(
        "status" in ix["column_names"] and "created_at" in ix["column_names"]
        for ix in indexes
    ), "缺少 (status, created_at) 复合索引"


def test_profile_version_packages_composite_pk(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    inspector = sa.inspect(engine)
    pks = set(inspector.get_pk_constraint("profile_version_packages")["constrained_columns"])
    assert pks == {"environment_version_id", "package_catalog_id"}


def test_fk_references_exist(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    inspector = sa.inspect(engine)
    fks = inspector.get_foreign_keys("environment_versions")
    assert any(fk["referred_table"] == "environment_profiles" for fk in fks)
    assert any(fk["referred_table"] == "environment_versions" for fk in fks)  # source_version_id 自引用
    pkg_fks = inspector.get_foreign_keys("package_catalog")
    assert any(fk["referred_table"] == "users" for fk in pkg_fks)
    job_fks = inspector.get_foreign_keys("environment_build_jobs")
    assert any(fk["referred_table"] == "environment_versions" for fk in job_fks)
    assert any(fk["referred_table"] == "environment_build_jobs" for fk in job_fks)  # retry_of_id 自引用


def test_upgrade_insert_smoke(engine):
    """迁移后可插入基础行（含 JSON 列与用户 FK）"""
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (1, 'admin', 'admin')"))
        conn.execute(
            sa.text(
                "INSERT INTO package_catalog (id, normalized_name, pip_name, locked_version,"
                " import_names, category_tags, source_key, status, created_by_id)"
                " VALUES (1, 'numpy', 'numpy', '2.1.3', '[\"numpy\"]', '[\"data\"]', 'pypi', 'active', 1)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO environment_profiles (id, slug, display_name, status)"
                " VALUES (1, 'basic', 'Python 基础', 'active')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO environment_versions (id, profile_id, version_number, status,"
                " base_image_ref, minimum_memory_mb, manifest_sha256)"
                " VALUES (1, 1, 1, 'draft', 'python:3.12-slim', 256, '%s')" % ("m" * 64)
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO profile_version_packages (environment_version_id, package_catalog_id, display_order)"
                " VALUES (1, 1, 0)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO environment_build_jobs (id, environment_version_id, status, attempt_number)"
                " VALUES (1, 1, 'queued', 1)"
            )
        )
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM environment_versions")).scalar() == 1
        assert conn.execute(sa.text("SELECT COUNT(*) FROM profile_version_packages")).scalar() == 1


def test_upgrade_enforces_unique_package(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO package_catalog (id, normalized_name, pip_name, locked_version,"
                " import_names, category_tags, source_key, status)"
                " VALUES (1, 'numpy', 'numpy', '2.1.3', '[]', '[]', 'pypi', 'active')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO package_catalog (id, normalized_name, pip_name, locked_version,"
                    " import_names, category_tags, source_key, status)"
                    " VALUES (2, 'numpy', 'numpy', '2.1.3', '[]', '[]', 'pypi', 'active')"
                )
            )


# ═══════════════════════════════════════════════════════════════
# downgrade
# ═══════════════════════════════════════════════════════════════

def test_downgrade_drops_all_control_tables(engine):
    migration = _load_migration()
    _minimal_schema(engine)
    _run_upgrade(engine, migration)
    _run_downgrade(engine, migration)
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    for t in CONTROL_TABLES:
        assert t not in tables, f"降级后 {t} 应被删除"
    assert "users" in tables  # 前置表保留


# ═══════════════════════════════════════════════════════════════
# 全链验证（隔离 SQLite 文件库，不污染开发库）
# ═══════════════════════════════════════════════════════════════

def _run_alembic_command(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # 清 PYTHONPATH，避免加载 Hermes 包崩溃
    env["DAI_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["DAI_ENVIRONMENT"] = "development"
    env["DAI_ALLOW_ENVIRONMENT_V2_DOWNGRADE"] = "true"
    env["DAI_ALLOW_ACCOUNT_MANAGEMENT_DOWNGRADE"] = "true"
    env["DAI_ALLOW_LAB_DOWNGRADE"] = "true"
    env["DAI_ALLOW_TEACHING_DOWNGRADE"] = "1"  # This helper always targets its disposable tmp_path SQLite file.
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _upgrade_full_chain(db_path: Path) -> None:
    """全链升级（分段）：base → 迁移 A → 插入 basic v1 种子 → head（迁移 B）。

    迁移 B 前置要求 basic 档位存在 available 版本且 image_digest 非空
    （plan 5：不满足时主动失败），因此真实部署顺序是"先跑迁移 A + seed build，
    再部署迁移 B"——测试按此分段执行。
    """
    up_a = _run_alembic_command(db_path, "upgrade", REVISION_A)
    assert up_a.returncode == 0, f"upgrade 迁移 A 失败:\n{up_a.stdout}\n{up_a.stderr}"
    _insert_basic_available_into_file(db_path)
    up_head = _run_alembic_command(db_path, "upgrade", "head")
    assert up_head.returncode == 0, f"upgrade head（迁移 B）失败:\n{up_head.stdout}\n{up_head.stderr}"


def _insert_basic_available_into_file(db_path: Path) -> None:
    """向隔离 SQLite 文件库插入 basic v1 available（image_digest 非空）。"""
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO environment_profiles (id, slug, display_name, status)"
                    " VALUES (1, 'basic', 'Python 基础', 'active')"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO environment_versions (id, profile_id, version_number, status,"
                    " base_image_ref, image_digest, minimum_memory_mb, manifest_sha256)"
                    " VALUES (1, 1, 1, 'available', 'python:3.12-slim@sha256:0000',"
                    " :digest, 256, 'm' * 64)"
                ).bindparams(digest="sha256:" + "a" * 64)
            )
    finally:
        engine.dispose()


def test_full_migration_chain_to_new_head(tmp_path):
    """从 base 全链 upgrade 到仓库当前 head，并保留环境绑定修复语义。"""
    db_path = tmp_path / "migration_chain.db"
    _upgrade_full_chain(db_path)

    current = _run_alembic_command(db_path, "current")
    assert current.returncode == 0, current.stderr
    assert CURRENT_HEAD in current.stdout, f"current 未到新 head: {current.stdout}"

    # 修复迁移语义：judge_questions.environment_version_id 可空（NULL=继承作业默认）；
    # 其余业务表绑定列保持 NOT NULL（创建链路均有服务层 basic 兜底或复制来源）
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        inspector = sa.inspect(engine)
        for table, cols in BINDING_TABLES.items():
            for col_name in cols:
                info = next(c for c in inspector.get_columns(table) if c["name"] == col_name)
                expected_nullable = (table == "judge_questions" and col_name == "environment_version_id")
                assert info["nullable"] is expected_nullable, (
                    f"{table}.{col_name} nullable 应为 {expected_nullable}（题目环境 NULL=继承作业默认）"
                )
    finally:
        engine.dispose()


def test_full_migration_chain_downgrade_keeps_previous_head(tmp_path):
    """全链 upgrade 后 downgrade 到 b4c5d6e7f890 可回滚：绑定列被删、控制面表保留"""
    db_path = tmp_path / "migration_chain_dg.db"
    _upgrade_full_chain(db_path)

    down = _run_alembic_command(db_path, "downgrade", REVISION_A)
    assert down.returncode == 0, f"downgrade 失败:\n{down.stdout}\n{down.stderr}"

    current = _run_alembic_command(db_path, "current")
    assert REVISION_A in current.stdout, current.stdout

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
    for t in CONTROL_TABLES:
        assert t in tables, f"降级到迁移 A 后控制面表 {t} 应保留"
    for table in BINDING_TABLES:
        cols = {c["name"] for c in sa.inspect(engine).get_columns(table)}
        for col in BINDING_TABLES[table]:
            assert col not in cols, f"降级后 {table}.{col} 应被删除"


# ═══════════════════════════════════════════════════════════════
# 迁移 B：业务绑定（Phase 3）
# ═══════════════════════════════════════════════════════════════

def _load_migration_b():
    spec = importlib.util.spec_from_file_location("env_bind_migration", MIGRATION_B_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _business_schema(engine) -> None:
    """迁移 B 单迁移测试所需的最小业务表（迁移前 schema：无绑定列）。

    使用裸列（不含 FK/约束）——SQLite 不强制外键，迁移 B 的 batch 重建表
    会补充 FK 与索引；列类型按 models 中业务表实际定义。
    """
    meta = sa.MetaData()
    sa.Table(
        "courses", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200)),
        sa.Column("status", sa.String(30)),
    )
    sa.Table(
        "assignments", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("course_id", sa.Integer()),
        sa.Column("title", sa.String(200)),
        sa.Column("status", sa.String(30)),
    )
    sa.Table(
        "judge_questions", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer()),
        sa.Column("title", sa.String(200)),
        sa.Column("function_name", sa.String(120)),
        sa.Column("hidden_tests", sa.Text()),
    )
    sa.Table(
        "submissions", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("question_id", sa.Integer()),
        sa.Column("student_id", sa.Integer()),
        sa.Column("code", sa.Text()),
        sa.Column("status", sa.String(40)),
    )
    sa.Table(
        "notebook_templates", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200)),
        sa.Column("status", sa.String(20)),
        sa.Column("owner_id", sa.Integer()),
    )
    sa.Table(
        "notebook_template_versions", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.Integer()),
        sa.Column("version_number", sa.Integer()),
        sa.Column("sha256", sa.String(64)),
        sa.Column("published_by_id", sa.Integer()),
    )
    sa.Table(
        "experiment_records", meta,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_version_id", sa.Integer()),
        sa.Column("student_id", sa.Integer()),
    )
    meta.create_all(engine)


def _insert_basic_available(engine) -> int:
    """插入 basic v1 available（image_digest 非空），返回版本 id。"""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO environment_profiles (id, slug, display_name, status)"
                " VALUES (1, 'basic', 'Python 基础', 'active')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO environment_versions (id, profile_id, version_number, status,"
                " base_image_ref, image_digest, minimum_memory_mb, manifest_sha256)"
                " VALUES (1, 1, 1, 'available', 'python:3.12-slim@sha256:0000',"
                " :digest, 256, 'm' * 64)"
            ).bindparams(digest="sha256:" + "a" * 64)
        )
    return 1


def _insert_business_rows(engine) -> None:
    """插入迁移前存量业务行（无环境绑定）。"""
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (1, 't', 'teacher')"))
        conn.execute(sa.text("INSERT INTO courses (id, title, status) VALUES (1, 'C1', 'published')"))
        conn.execute(sa.text(
            "INSERT INTO assignments (id, course_id, title, status) VALUES (1, 1, 'A1', 'published')"
        ))
        conn.execute(sa.text(
            "INSERT INTO judge_questions (id, assignment_id, title, function_name, hidden_tests)"
            " VALUES (1, 1, 'Q1', 'add', 'assert True')"
        ))
        conn.execute(sa.text(
            "INSERT INTO submissions (id, question_id, student_id, code, status)"
            " VALUES (1, 1, 1, 'print(1)', 'completed')"
        ))
        conn.execute(sa.text(
            "INSERT INTO notebook_templates (id, name, status, owner_id)"
            " VALUES (1, 'N1', 'published', 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO notebook_template_versions (id, template_id, version_number, sha256, published_by_id)"
            " VALUES (1, 1, 1, 's' * 64, 1)"
        ))
        conn.execute(sa.text(
            "INSERT INTO experiment_records (id, template_version_id, student_id) VALUES (1, 1, 1)"
        ))


def test_revision_b_chain_points_to_head_a():
    migration = _load_migration_b()
    assert migration.revision == REVISION_B
    assert migration.down_revision == REVISION_A
    assert migration.branch_labels is None
    assert migration.depends_on is None


def test_revision_fix_points_to_head_b():
    """修复迁移 d6e7f8a9b012 挂在迁移 B 之后。"""
    migration = _load_migration_fix()
    assert migration.revision == REVISION_FIX
    assert migration.down_revision == REVISION_B
    assert migration.branch_labels is None
    assert migration.depends_on is None


def _load_migration_fix():
    spec = importlib.util.spec_from_file_location("judge_env_nullable_fix", MIGRATION_FIX_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_b_requires_basic_available(engine):
    """无 basic 可用版本时迁移 B 必须主动失败（fail-closed，plan 5 步骤 1）"""
    migration_a = _load_migration()
    migration_b = _load_migration_b()
    _minimal_schema(engine)
    _run_upgrade(engine, migration_a)
    _business_schema(engine)
    with pytest.raises(RuntimeError):
        _run_upgrade(engine, migration_b)


def test_upgrade_b_adds_columns_and_backfills(engine):
    """迁移 B：加绑定列并回填存量数据（basic + unrestricted/inherit）"""
    migration_a = _load_migration()
    migration_b = _load_migration_b()
    _minimal_schema(engine)
    _run_upgrade(engine, migration_a)
    _insert_basic_available(engine)
    _business_schema(engine)
    _insert_business_rows(engine)
    _run_upgrade(engine, migration_b)

    inspector = sa.inspect(engine)
    for table, cols in BINDING_TABLES.items():
        col_names = {c["name"] for c in inspector.get_columns(table)}
        for col in cols:
            assert col in col_names, f"{table} 缺少绑定列 {col}"

    with engine.connect() as conn:
        basic_id = conn.execute(sa.text(
            "SELECT id FROM environment_versions WHERE version_number = 1"
        )).scalar()
        # 作业：basic + unrestricted + 空白名单
        row = conn.execute(sa.text(
            "SELECT environment_version_id, import_policy_mode, allowed_imports"
            " FROM assignments WHERE id = 1"
        )).one()
        assert row[0] == basic_id
        assert row[1] == "unrestricted"
        assert row[2] == "[]"
        # 题目：basic + inherit
        row = conn.execute(sa.text(
            "SELECT environment_version_id, import_policy_mode, allowed_imports"
            " FROM judge_questions WHERE id = 1"
        )).one()
        assert row[0] == basic_id
        assert row[1] == "inherit"
        assert row[2] == "[]"
        # 提交：环境快照
        row = conn.execute(sa.text(
            "SELECT environment_version_id, import_policy_mode_snapshot, allowed_imports_snapshot"
            " FROM submissions WHERE id = 1"
        )).one()
        assert row[0] == basic_id
        assert row[1] == "unrestricted"
        assert row[2] == "[]"
        # Notebook 草稿
        row = conn.execute(sa.text(
            "SELECT draft_environment_version_id, draft_import_policy_mode, draft_allowed_imports"
            " FROM notebook_templates WHERE id = 1"
        )).one()
        assert row[0] == basic_id
        assert row[1] == "unrestricted"
        assert row[2] == "[]"
        # Notebook 历史版本
        row = conn.execute(sa.text(
            "SELECT environment_version_id, import_policy_mode, allowed_imports"
            " FROM notebook_template_versions WHERE id = 1"
        )).one()
        assert row[0] == basic_id
        assert row[1] == "unrestricted"
        assert row[2] == "[]"
        # 实验记录：优先复制模板版本环境
        row = conn.execute(sa.text(
            "SELECT environment_version_id FROM experiment_records WHERE id = 1"
        )).one()
        assert row[0] == basic_id


def test_upgrade_b_sets_not_null_and_fk(engine):
    migration_a = _load_migration()
    migration_b = _load_migration_b()
    _minimal_schema(engine)
    _run_upgrade(engine, migration_a)
    _insert_basic_available(engine)
    _business_schema(engine)
    _insert_business_rows(engine)
    _run_upgrade(engine, migration_b)

    inspector = sa.inspect(engine)
    for table, cols in BINDING_TABLES.items():
        for col in cols:
            col_info = next(c for c in inspector.get_columns(table) if c["name"] == col)
            assert col_info["nullable"] is False, f"{table}.{col} 应为 NOT NULL"
        if table != "experiment_records":
            fks = inspector.get_foreign_keys(table)
            assert any(fk["referred_table"] == "environment_versions" for fk in fks), \
                f"{table} 缺少指向 environment_versions 的外键"
            indexes = inspector.get_indexes(table)
            env_cols = [c for c in cols if c.endswith("environment_version_id")]
            assert any(set(ix["column_names"]) == set(env_cols) for ix in indexes), \
                f"{table} 缺少环境版本索引"


def test_upgrade_b_falls_back_to_basic_for_orphan_records(engine):
    """实验记录模板版本缺失（孤儿记录）时兜底绑定 basic，迁移不中断"""
    migration_a = _load_migration()
    migration_b = _load_migration_b()
    _minimal_schema(engine)
    _run_upgrade(engine, migration_a)
    _insert_basic_available(engine)
    _business_schema(engine)
    _insert_business_rows(engine)
    # 一条 template_version_id 指向不存在行的孤儿记录（防御数据异常）
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO experiment_records (id, template_version_id, student_id) VALUES (2, 999, 1)"
        ))
    _run_upgrade(engine, migration_b)
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT environment_version_id FROM experiment_records WHERE id = 2"
        )).one()
        assert row[0] == 1, "孤儿实验记录应兜底绑定 basic v1"


def test_downgrade_b_drops_only_binding_columns(engine):
    """降级只删新增业务绑定列，不删控制面表与原始数据（plan 5）"""
    migration_a = _load_migration()
    migration_b = _load_migration_b()
    _minimal_schema(engine)
    _run_upgrade(engine, migration_a)
    _insert_basic_available(engine)
    _business_schema(engine)
    _insert_business_rows(engine)
    _run_upgrade(engine, migration_b)
    _run_downgrade(engine, migration_b)

    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    for t in CONTROL_TABLES:
        assert t in tables, f"降级后控制面表 {t} 应保留"
    for table, cols in BINDING_TABLES.items():
        col_names = {c["name"] for c in inspector.get_columns(table)}
        for col in cols:
            assert col not in col_names, f"降级后 {table}.{col} 应被删除"
    # 原始业务数据完好
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM assignments")).scalar() == 1
        assert conn.execute(sa.text("SELECT COUNT(*) FROM experiment_records")).scalar() == 1
