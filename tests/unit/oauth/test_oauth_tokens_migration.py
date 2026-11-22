import sqlite3
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory

from pmsbridge.core.settings import Settings
from pmsbridge.core.storage import alembic_config, migrate, prepare_storage

ADDING_REVISION = "d4b8e2a7c015"


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_the_tokens_table_is_added_and_removed_by_its_migration(dev_settings: Settings) -> None:
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(ADDING_REVISION)
    assert revision is not None and isinstance(revision.down_revision, str)
    prepare_storage(dev_settings)
    command.upgrade(config, revision.down_revision)
    assert "oauth_tokens" not in tables(dev_settings.database_path)
    assert "last_used_at" not in columns(dev_settings.database_path, "oauth_grants")
    migrate(dev_settings)
    assert "oauth_tokens" in tables(dev_settings.database_path)
    assert "last_used_at" in columns(dev_settings.database_path, "oauth_grants")
    command.downgrade(config, revision.down_revision)
    assert "oauth_tokens" not in tables(dev_settings.database_path)
    assert "last_used_at" not in columns(dev_settings.database_path, "oauth_grants")
    assert "oauth_grants" in tables(dev_settings.database_path)
    with sqlite3.connect(dev_settings.database_path) as connection:
        (create_sql,) = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'oauth_grants'"
        ).fetchone()
    assert "AUTOINCREMENT" in create_sql.upper()
