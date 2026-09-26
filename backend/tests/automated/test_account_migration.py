"""Additive migration preserves existing credentials and works on SQLite too."""
import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_existing_account_retains_credentials_and_defaults(tmp_path):
    path = Path(__file__).parents[2] / 'alembic/versions/20260921_0001_account_management.py'
    spec = importlib.util.spec_from_file_location('account_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(f'sqlite:///{tmp_path}/migration.db')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY, password_hash VARCHAR(255), session_version INTEGER)')
        connection.exec_driver_sql('CREATE TABLE teaching_class_students (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql("INSERT INTO users VALUES (1, 'existing-hash', 17)")
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        row = connection.execute(sa.text('SELECT password_hash, session_version, credential_version, must_change_password FROM users')).one()
        assert tuple(row) == ('existing-hash', 17, 1, 0)
        connection.execute(sa.text('INSERT INTO account_security_policies (id) VALUES (1)'))
        assert connection.scalar(sa.text('SELECT created_at FROM account_security_policies')) is not None
