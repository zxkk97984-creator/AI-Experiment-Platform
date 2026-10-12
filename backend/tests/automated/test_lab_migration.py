"""Validate Lab and School migrations against their actual historical phases."""

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.database import Base
import app.models  # noqa: F401
from migration_stage_support import (
    assert_head_metadata,
    assert_historical_integer_types,
    historical_schema,
    load_revision,
    upgrade_to_current_head,
)


def test_lab_migration_is_additive_and_matches_metadata(tmp_path, monkeypatch):
    path = tmp_path / 'migration.db'
    historical_schema(path, '20260921_0001')
    # Alembic's SQLite batch rebuilds operate with foreign_keys disabled; inspect
    # referential integrity explicitly after rebuilding instead of using latest
    # ORM metadata as a pretend pre-Lab schema.
    engine = create_engine(f'sqlite:///{path}')
    migration = load_revision('20260926_0001')
    teaching = load_revision('20261003_0001')
    with engine.begin() as conn:
        assert_historical_integer_types(conn)
        assert not any(t.startswith('lab_') for t in inspect(conn).get_table_names())
        conn.execute(text("INSERT INTO users (id,username,password_hash,role,real_name,status,session_version,credential_version,must_change_password) VALUES (999,'migration-user','unchanged','student','Migration','active',1,1,0)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert conn.execute(text('SELECT password_hash FROM users WHERE id=999')).scalar() == 'unchanged'
        assert set(t for t in inspect(conn).get_table_names() if t.startswith('lab_')) == set(t for t in Base.metadata.tables if t.startswith('lab_'))
        assert 'offering_id' not in {c['name'] for c in inspect(conn).get_columns('lab_regrade_batches')}
        assert 'course_offerings' not in inspect(conn).get_table_names()
        with Operations.context(MigrationContext.configure(conn)):
            teaching.upgrade()
        assert 'offering_id' in {c['name'] for c in inspect(conn).get_columns('lab_regrade_batches')}
    engine.dispose()

    # 历史阶段手动应用完成后，用 Alembic 自身收敛到当前 head：后续新增迁移
    # （认领代次、结果修订号等）自动纳入，不再硬编码某一个 revision 号。
    upgrade_to_current_head(path, stamped_at='20261003_0001')

    engine = create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        assert_head_metadata(conn)
        assert conn.execute(text('PRAGMA foreign_key_check')).all() == []
        assert conn.execute(text('SELECT password_hash FROM users WHERE id=999')).scalar() == 'unchanged'
        monkeypatch.setenv('DAI_ALLOW_TEACHING_DOWNGRADE','1')
        monkeypatch.setenv('DAI_ALLOW_LAB_DOWNGRADE','true')
        monkeypatch.setenv('DAI_ENVIRONMENT','development')
        with Operations.context(MigrationContext.configure(conn)):
            teaching.downgrade()
        assert 'offering_id' not in {c['name'] for c in inspect(conn).get_columns('lab_regrade_batches')}
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert not any(t.startswith('lab_') for t in inspect(conn).get_table_names())
        assert conn.execute(text('SELECT username FROM users WHERE id=999')).scalar() == 'migration-user'
    engine.dispose()
    engine.dispose()
