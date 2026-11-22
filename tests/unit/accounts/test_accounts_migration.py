import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from pmsbridge.core.settings import Settings
from pmsbridge.core.storage import (
    Base,
    alembic_config,
    import_model_modules,
    migrate,
    prepare_storage,
)

ACCOUNTS_REVISION = "7c4d2a91f3b8"


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_the_accounts_tables_are_created_on_a_fresh_database(dev_settings: Settings) -> None:
    migrate(dev_settings)
    assert {"users", "sessions"} <= tables(dev_settings.database_path)


def test_the_accounts_migration_upgrades_from_its_parent_revision(dev_settings: Settings) -> None:
    config = alembic_config(dev_settings.database_path)
    parent = ScriptDirectory.from_config(config).get_revision(ACCOUNTS_REVISION)
    assert parent is not None
    assert isinstance(parent.down_revision, str)
    prepare_storage(dev_settings)
    command.upgrade(config, parent.down_revision)
    assert not {"users", "sessions"} & tables(dev_settings.database_path)
    result = migrate(dev_settings)
    assert result.backup_path is not None
    assert result.revisions_after == tuple(sorted(ScriptDirectory.from_config(config).get_heads()))
    assert {"users", "sessions"} <= tables(dev_settings.database_path)


def test_the_migration_matches_the_models(dev_settings: Settings) -> None:
    """Fails when a model changes without a migration (or the other way round)."""
    migrate(dev_settings)
    import_model_modules()
    engine = create_engine(f"sqlite:///{dev_settings.database_path}")
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)
    engine.dispose()
    ours = [d for d in differences if "users" in repr(d) or "sessions" in repr(d)]
    assert ours == []


TWO_FACTOR_REVISION = "b81e5f3a60c2"
TWO_FACTOR_TABLES = {"one_time_tokens", "auth_flows", "user_totp", "recovery_codes"}


def test_the_two_factor_migration_upgrades_from_the_accounts_revision(
    dev_settings: Settings,
) -> None:
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(TWO_FACTOR_REVISION)
    assert revision is not None
    assert revision.down_revision == ACCOUNTS_REVISION
    prepare_storage(dev_settings)
    command.upgrade(config, ACCOUNTS_REVISION)
    with sqlite3.connect(dev_settings.database_path) as connection:
        connection.execute(
            "INSERT INTO users (email, password_hash, role, is_active, created_at) "
            "VALUES ('a@example.test', 'x', 'admin', 1, '2026-01-01 00:00:00')"
        )
    assert not TWO_FACTOR_TABLES & tables(dev_settings.database_path)
    migrate(dev_settings)
    assert tables(dev_settings.database_path) >= TWO_FACTOR_TABLES
    with sqlite3.connect(dev_settings.database_path) as connection:
        row = connection.execute("SELECT is_active, setup_pending FROM users").fetchone()
    assert row == (1, 0)  # existing users stay active and are not pending setup


def test_the_two_factor_migration_matches_the_models_and_can_be_undone(
    dev_settings: Settings,
) -> None:
    migrate(dev_settings)
    import_model_modules()
    engine = create_engine(f"sqlite:///{dev_settings.database_path}")
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)
    engine.dispose()
    names = TWO_FACTOR_TABLES | {"users"}
    assert [d for d in differences if any(name in repr(d) for name in names)] == []
    config = alembic_config(dev_settings.database_path)
    command.downgrade(config, ACCOUNTS_REVISION)
    assert not TWO_FACTOR_TABLES & tables(dev_settings.database_path)
