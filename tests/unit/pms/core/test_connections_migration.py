"""The migration that adds the per-connection switch for changes."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from pmsbridge.core.settings import Settings
from pmsbridge.core.storage import alembic_config, migrate, prepare_storage

REVISION = "b3e7a9c1d5f8"


@pytest.fixture
def dev_settings(tmp_path: Path) -> Settings:
    return Settings(environment="development", data_dir=tmp_path / "migration-data")


def columns(settings: Settings) -> dict[str, tuple[int, str | None]]:
    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute("PRAGMA table_info(pms_connections)").fetchall()
    return {row[1]: (row[3], row[4]) for row in rows}


def test_the_history_has_one_head(dev_settings: Settings) -> None:
    script = ScriptDirectory.from_config(alembic_config(dev_settings.database_path))
    assert len(script.get_heads()) == 1


def test_changes_are_off_for_connections_that_existed_before(dev_settings: Settings) -> None:
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(REVISION)
    assert revision is not None and isinstance(revision.down_revision, str)
    prepare_storage(dev_settings)
    command.upgrade(config, revision.down_revision)
    assert "writes_enabled" not in columns(dev_settings)
    with sqlite3.connect(dev_settings.database_path) as connection:
        connection.execute(
            "INSERT INTO pms_connections (id, provider, label, status, credentials, "
            "client_id_hint, client_id_digest, created_at, updated_at) VALUES "
            "('a', 'guesty', 'Old', 'active', x'00', '1234', 'd', '2026-01-01', '2026-01-01')"
        )
    migrate(dev_settings)
    assert columns(dev_settings)["writes_enabled"][0] == 1  # NOT NULL
    with sqlite3.connect(dev_settings.database_path) as connection:
        found = connection.execute("SELECT writes_enabled FROM pms_connections").fetchall()
    assert found == [(0,)]


def test_the_migration_can_be_undone(dev_settings: Settings) -> None:
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(REVISION)
    assert revision is not None and isinstance(revision.down_revision, str)
    prepare_storage(dev_settings)
    command.upgrade(config, REVISION)
    assert "writes_enabled" in columns(dev_settings)
    command.downgrade(config, revision.down_revision)
    assert "writes_enabled" not in columns(dev_settings)
