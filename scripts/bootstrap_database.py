#!/usr/bin/env python3
"""Run the supported, idempotent two-stage database bootstrap.

The environment binding migration requires a usable ``basic`` environment
version.  This entrypoint is the only supported caller for a fresh or
partially migrated database:

    base -> b4c5d6e7f890 -> basic seed -> head

Production requires a real, pre-validated basic image digest.  Development
and CI disposable smoke may omit it and receive the explicit disposable seed
digest from the seed module.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory


REVISION_A = "b4c5d6e7f890"


class BootstrapError(RuntimeError):
    """Raised when the database cannot safely follow the supported path."""


def _backend_dir() -> Path:
    configured = os.environ.get("DAI_BACKEND_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    current = Path.cwd().resolve()
    if (current / "alembic.ini").is_file():
        return current
    return (Path(__file__).resolve().parents[1] / "backend").resolve()


def _database_url(backend_dir: Path) -> str:
    raw = os.environ.get("DAI_DATABASE_URL", "").strip()
    if raw:
        return raw
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    try:
        from app.config import Settings

        return Settings().database_url
    except Exception as exc:  # pragma: no cover - configuration failures vary by host
        raise BootstrapError("DAI_DATABASE_URL 未设置，且无法从应用配置读取数据库 URL") from exc


def _load_seed_module() -> ModuleType:
    seed_path = Path(__file__).with_name("seed-basic-environment-mysql.py")
    spec = importlib.util.spec_from_file_location("basic_environment_seed", seed_path)
    if spec is None or spec.loader is None:
        raise BootstrapError(f"找不到 seed 脚本: {seed_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory(backend_dir: Path) -> ScriptDirectory:
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return ScriptDirectory.from_config(config)


def _alembic_config(backend_dir: Path, database_url: str) -> Config:
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    # ConfigParser treats '%' as interpolation syntax; environment URLs may
    # contain an encoded percent in a password.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _current_revision(database_url: str) -> str | None:
    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as connection:
            if not sa.inspect(connection).has_table("alembic_version"):
                return None
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _is_ancestor(script: ScriptDirectory, ancestor: str | None, descendant: str | None) -> bool:
    """Return whether a revision is on the linear migration path to another."""
    if ancestor is None:
        return True
    if descendant is None:
        return False
    revision = script.get_revision(descendant)
    while revision is not None:
        if revision.revision == ancestor:
            return True
        down_revision = revision.down_revision
        if isinstance(down_revision, tuple):
            return ancestor in down_revision
        revision = script.get_revision(down_revision) if down_revision else None
    return False


def _upgrade(backend_dir: Path, database_url: str, target: str) -> None:
    command.upgrade(_alembic_config(backend_dir, database_url), target)


def bootstrap_database() -> str:
    """Execute the safe migration sequence and return the final revision."""
    backend_dir = _backend_dir()
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    database_url = _database_url(backend_dir)
    seed = _load_seed_module()
    environment = os.environ.get("DAI_ENVIRONMENT", "development")
    migration_mode = seed.resolve_migration_environment(
        environment=environment,
        migration_mode=os.environ.get("DAI_MIGRATION_MODE", ""),
    )

    # Resolve digest before opening a database connection.  A production
    # deployment with missing/placeholder evidence must fail deterministically.
    seed.resolve_image_digest(
        environment=migration_mode,
        raw_digest=os.environ.get("DAI_BASIC_ENVIRONMENT_IMAGE_DIGEST", ""),
    )

    script = _script_directory(backend_dir)
    head = script.get_current_head()
    if not head:
        raise BootstrapError("Alembic 没有可用 head")

    current = _current_revision(database_url)
    if current == head:
        print(f"数据库已在 head ({head})，无需迁移")
        return head
    if current is not None and script.get_revision(current) is None:
        raise BootstrapError(f"数据库 revision {current} 不在当前迁移链中")

    if current is None or _is_ancestor(script, current, REVISION_A):
        print(f"执行迁移 A: {current or 'base'} -> {REVISION_A}")
        _upgrade(backend_dir, database_url, REVISION_A)
        current = _current_revision(database_url)

    if current == REVISION_A:
        print("执行幂等 basic seed")
        profile_id = seed.seed_basic_environment(
            database_url=database_url,
            environment=migration_mode,
            raw_digest=os.environ.get("DAI_BASIC_ENVIRONMENT_IMAGE_DIGEST", ""),
            base_image_ref=os.environ.get(
                "DAI_BASIC_ENVIRONMENT_BASE_IMAGE",
                os.environ.get(
                    "DAI_ENV_BASE_IMAGE",
                    "python:3.12-slim@sha256:" + "0" * 64,
                ),
            ),
        )
        print(f"basic seed 就绪 (profile_id={profile_id})")
        current = _current_revision(database_url)

    if current != head:
        if current is None or not _is_ancestor(script, current, head):
            raise BootstrapError(
                f"数据库当前 revision {current!r} 无法安全升级到 head {head}"
            )
        print(f"执行迁移 B 及后续迁移: {current or 'base'} -> {head}")
        _upgrade(backend_dir, database_url, head)
        current = _current_revision(database_url)

    if current != head:
        raise BootstrapError(f"迁移完成后 revision 为 {current!r}，预期 {head}")
    return head


def main() -> int:
    try:
        bootstrap_database()
    except Exception as exc:  # keep service dependency failure concise and fail-closed
        print(f"数据库 bootstrap 失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
