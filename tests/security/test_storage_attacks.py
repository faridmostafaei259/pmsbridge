"""Attacks on the SQLite layer: hostile database files, tampered revisions, leaked parameters."""

import hashlib
import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from pmsbridge.core.settings import Settings
from pmsbridge.core.storage import Database, StorageError, migrate, prepare_storage

SENTINEL = "Zq9-sentinel-value-4711"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*")}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(environment="development", data_dir=tmp_path / "data")


def test_a_file_that_is_not_a_database_is_refused_and_left_untouched(settings: Settings) -> None:
    prepare_storage(settings)
    settings.database_path.write_bytes(b"not a database " + SENTINEL.encode() * 100)
    before = digest(settings.database_path)
    with pytest.raises(StorageError) as raised:
        migrate(settings)
    assert digest(settings.database_path) == before
    assert SENTINEL not in str(raised.value)
    assert not list(settings.backup_dir.iterdir())


@pytest.mark.parametrize(
    "revision",
    [
        "../../../../tmp/escape",
        "..\\..\\escape",
        "a/b",
        "x'; DROP TABLE alembic_version; --",
        "\x00",
        "a" * 5000,
    ],
)
def test_a_tampered_revision_cannot_steer_a_backup_outside_the_backup_folder(
    settings: Settings, tmp_path: Path, revision: str
) -> None:
    migrate(settings)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE alembic_version SET version_num = ?", (revision,))
    outside_before = files_under(tmp_path) - files_under(settings.data_dir)
    with pytest.raises(StorageError):
        migrate(settings)
    assert files_under(tmp_path) - files_under(settings.data_dir) == outside_before
    for created in settings.backup_dir.iterdir():
        assert created.parent == settings.backup_dir


def test_a_database_from_a_newer_version_is_refused_before_anything_is_written(
    settings: Settings,
) -> None:
    migrate(settings)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
    backups_before = set(settings.backup_dir.iterdir())
    content = {
        row
        for row in sqlite3.connect(settings.database_path).execute("SELECT * FROM alembic_version")
    }
    with pytest.raises(StorageError, match="newer version"):
        migrate(settings)
    assert set(settings.backup_dir.iterdir()) == backups_before
    assert content == {
        row
        for row in sqlite3.connect(settings.database_path).execute("SELECT * FROM alembic_version")
    }


async def test_bound_values_do_not_appear_in_database_errors(settings: Settings) -> None:
    migrate(settings)
    database = Database(settings.database_path)
    try:
        async with database.session() as session:
            with pytest.raises(DBAPIError) as raised:
                await session.execute(
                    text("SELECT * FROM table_that_does_not_exist WHERE x = :value"),
                    {"value": SENTINEL},
                )
        assert SENTINEL not in str(raised.value)
        assert SENTINEL not in repr(raised.value)
        async with database.write_transaction() as writer:
            with pytest.raises(DBAPIError) as raised_write:
                await writer.execute(
                    text("INSERT INTO table_that_does_not_exist (x) VALUES (:value)"),
                    {"value": SENTINEL},
                )
        assert SENTINEL not in str(raised_write.value)
    finally:
        await database.dispose()


async def test_every_file_the_database_creates_is_private(settings: Settings) -> None:
    migrate(settings)
    database = Database(settings.database_path)
    try:
        async with database.write_transaction() as writer:
            await writer.execute(text("CREATE TABLE IF NOT EXISTS probe (x INTEGER)"))
        async with database.session() as reader:
            await reader.execute(text("SELECT count(*) FROM probe"))
        found = [p for p in settings.data_dir.rglob("*") if p.is_file() or p.is_dir()]
        assert found
        for path in [settings.data_dir, *found]:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & 0o077 == 0, f"{path} is accessible to group or others: {mode:o}"
    finally:
        await database.dispose()


def test_a_world_readable_data_folder_is_tightened_at_startup(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, mode=0o755)
    settings.data_dir.chmod(0o755)
    prepare_storage(settings)
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.database_path.stat().st_mode) == 0o600


def test_a_world_readable_database_file_is_tightened_at_startup(settings: Settings) -> None:
    prepare_storage(settings)
    settings.database_path.chmod(0o644)
    prepare_storage(settings)
    assert stat.S_IMODE(settings.database_path.stat().st_mode) == 0o600
