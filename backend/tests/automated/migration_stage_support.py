"""Build actual historical schemas in explicitly disposable databases."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.database import Base

BACKEND = Path(__file__).resolve().parents[2]
REVISION_A = 'b4c5d6e7f890'
SQLITE_HISTORICAL_BIGINT_COLUMNS = {
    ('environment_build_jobs', 'id'),
    ('environment_profiles', 'id'),
    ('environment_versions', 'id'),
    ('notification_reads', 'notification_id'),
    ('notifications', 'id'),
    ('package_catalog', 'id'),
}


def assert_historical_integer_types(connection):
    """These six pre-School migrations use BIGINT even on SQLite.

    Current ORM SQLite INTEGER variants permit generated primary keys. Verify
    the inherited representation explicitly rather than ignoring type diffs.
    """
    inspector = sa.inspect(connection)
    for table, name in SQLITE_HISTORICAL_BIGINT_COLUMNS:
        column = next(c for c in inspector.get_columns(table) if c['name'] == name)
        assert isinstance(column['type'], sa.BigInteger), (table, name, column['type'])


def assert_head_metadata(connection):
    differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    if connection.dialect.name != 'sqlite':
        assert differences == []
        return
    assert_historical_integer_types(connection)
    keys = set()
    for group in differences:
        assert isinstance(group, list) and len(group) == 1, group
        diff = group[0]
        assert diff[0] == 'modify_type', diff
        assert isinstance(diff[5], sa.BigInteger), diff
        assert isinstance(diff[6].dialect_impl(connection.dialect), sa.Integer), diff
        keys.add((diff[2], diff[3]))
    assert len(differences) == len(SQLITE_HISTORICAL_BIGINT_COLUMNS)
    assert keys == SQLITE_HISTORICAL_BIGINT_COLUMNS


def load_revision(revision):
    paths = list((BACKEND / 'alembic' / 'versions').glob(f'{revision}_*.py'))
    assert len(paths) == 1, f'Expected one migration for {revision}'
    spec = importlib.util.spec_from_file_location(f'test_migration_{revision}', paths[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migrate_url(url, *arguments):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['DAI_DATABASE_URL'] = url
    env['DAI_ENVIRONMENT'] = 'development'
    result = subprocess.run([sys.executable, '-m', 'alembic', *arguments], cwd=BACKEND,
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f'Historical migration failed:\n{result.stdout}\n{result.stderr}'


def migrate_file(path, *arguments):
    migrate_url(f'sqlite:///{path}', *arguments)


def upgrade_to_current_head(path, *, stamped_at: str):
    """历史阶段手动应用后，用 Alembic 自身把剩余 revision 收敛到当前 head。

    先 stamp 到已手动应用到的版本，再 ``upgrade head``：后续新增迁移自动纳入
    覆盖范围，不必在测试里硬编码"只调用某一个新的 revision"。真实环境依旧按
    同一命令执行迁移，因此这里验证的是与部署一致的路径。
    """
    migrate_file(path, 'stamp', stamped_at)
    migrate_file(path, 'upgrade', 'head')


def upgrade_url_to_current_head(url, *, stamped_at: str):
    """``upgrade_to_current_head`` 的显式数据库 URL 版本（MySQL 等）。"""
    migrate_url(url, 'stamp', stamped_at)
    migrate_url(url, 'upgrade', 'head')


def seed_environment(connection):
    connection.execute(sa.text("INSERT INTO environment_profiles (id, slug, display_name, status) VALUES (1, 'basic', 'Test basic', 'active')"))
    connection.execute(sa.text("""INSERT INTO environment_versions
        (id, profile_id, version_number, status, base_image_ref, image_digest, minimum_memory_mb, manifest_sha256)
        VALUES (1, 1, 1, 'available', :image, :digest, 256, :manifest)"""),
        {'image': 'python:3.12-slim@sha256:' + '0' * 64, 'digest': 'sha256:' + 'a' * 64, 'manifest': 'c' * 64})


def historical_schema(path, target):
    migrate_file(path, 'upgrade', REVISION_A)
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        seed_environment(conn)
    engine.dispose()
    migrate_file(path, 'upgrade', target)
