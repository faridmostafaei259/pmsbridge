import sqlite3
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory

from pmsbridge.core.settings import Settings
from pmsbridge.core.storage import alembic_config, migrate, prepare_storage

ADDING_REVISION = "f6c1a8d3e592"
NEW_COLUMNS = {"kind", "client_url", "fetched_at", "cache_until"}


def columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def table_sql(path: Path, table: str) -> str:
    with sqlite3.connect(path) as connection:
        (sql,) = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    return str(sql)


def test_the_history_still_has_exactly_one_head() -> None:
    heads = ScriptDirectory.from_config(alembic_config(Path("unused.db"))).get_heads()
    assert len(heads) == 1


def test_the_client_columns_are_added_and_removed_without_losing_clients_or_grants(
    dev_settings: Settings,
) -> None:
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(ADDING_REVISION)
    assert revision is not None and isinstance(revision.down_revision, str)
    prepare_storage(dev_settings)
    command.upgrade(config, revision.down_revision)
    path = dev_settings.database_path
    assert not NEW_COLUMNS & columns(path, "oauth_clients")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users"
            " (id, email, password_hash, role, is_active, setup_pending, created_at)"
            " VALUES (1, 'a@example.test', 'x', 'staff', 1, 0, '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO oauth_clients (client_id, client_name, redirect_uris,"
            " token_endpoint_auth_method, grant_types, created_at)"
            " VALUES ('c' || hex(randomblob(10)), 'Old', '[]', 'none', '[]', '2026-01-01 00:00:00')"
        )
    migrate(dev_settings)
    assert columns(path, "oauth_clients") >= NEW_COLUMNS
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT kind, client_url FROM oauth_clients").fetchall() == [
            ("dcr", None)
        ]
    command.downgrade(config, revision.down_revision)
    assert not NEW_COLUMNS & columns(path, "oauth_clients")
    assert "auth_method" in table_sql(path, "oauth_clients")  # the check constraint survives
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT client_name FROM oauth_clients").fetchall() == [("Old",)]
    migrate(dev_settings)
    assert columns(path, "oauth_clients") >= NEW_COLUMNS
