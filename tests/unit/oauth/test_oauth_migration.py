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

CLIENTS_REVISION = "c92d6e1f4a37"
OAUTH_TABLES = {"oauth_clients", "oauth_grants", "oauth_codes"}


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_the_history_has_one_head(dev_settings: Settings) -> None:
    script = ScriptDirectory.from_config(alembic_config(dev_settings.database_path))
    assert len(script.get_heads()) == 1


def test_the_oauth_tables_are_created_on_a_fresh_database(dev_settings: Settings) -> None:
    migrate(dev_settings)
    assert tables(dev_settings.database_path) >= OAUTH_TABLES


def test_the_oauth_migration_upgrades_from_its_parent_revision(dev_settings: Settings) -> None:
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(CLIENTS_REVISION)
    assert revision is not None
    assert isinstance(revision.down_revision, str)
    prepare_storage(dev_settings)
    command.upgrade(config, revision.down_revision)
    assert not OAUTH_TABLES & tables(dev_settings.database_path)
    result = migrate(dev_settings)
    assert result.revisions_after == tuple(sorted(ScriptDirectory.from_config(config).get_heads()))
    assert tables(dev_settings.database_path) >= OAUTH_TABLES


def test_the_oauth_migration_downgrades(dev_settings: Settings) -> None:
    migrate(dev_settings)
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(CLIENTS_REVISION)
    assert revision is not None
    assert isinstance(revision.down_revision, str)
    command.downgrade(config, revision.down_revision)
    assert not OAUTH_TABLES & tables(dev_settings.database_path)


def test_the_migration_matches_the_models(dev_settings: Settings) -> None:
    """Fails when a model changes without a migration (or the other way round)."""
    migrate(dev_settings)
    import_model_modules()
    engine = create_engine(f"sqlite:///{dev_settings.database_path}")
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)
    engine.dispose()
    ours = [d for d in differences if "oauth" in repr(d)]
    assert ours == []
