"""The audit log: the migration, what a row holds, and retention."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from sqlalchemy import select

from pmsbridge.core import audit
from pmsbridge.core.audit import AuditLog
from pmsbridge.core.settings import Settings, set_settings_override
from pmsbridge.core.storage import (
    alembic_config,
    migrate,
    prepare_storage,
    session,
    write_transaction,
)

REVISION = "a1c4e7b92d63"


@pytest.fixture
def dev_settings(tmp_path: Path) -> Settings:
    return Settings(environment="development", data_dir=tmp_path / "migration-data")


def tables(settings: Settings) -> set[str]:
    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_the_history_has_one_head(dev_settings: Settings) -> None:
    script = ScriptDirectory.from_config(alembic_config(dev_settings.database_path))
    assert len(script.get_heads()) == 1


def test_the_migration_adds_and_removes_the_table(dev_settings: Settings) -> None:
    config = alembic_config(dev_settings.database_path)
    revision = ScriptDirectory.from_config(config).get_revision(REVISION)
    assert revision is not None and isinstance(revision.down_revision, str)
    prepare_storage(dev_settings)
    command.upgrade(config, revision.down_revision)
    assert "audit_log" not in tables(dev_settings)
    migrate(dev_settings)
    assert "audit_log" in tables(dev_settings)
    with sqlite3.connect(dev_settings.database_path) as connection:
        names = [row[1] for row in connection.execute("PRAGMA table_info(audit_log)")]
        indexes = [row[1] for row in connection.execute("PRAGMA index_list(audit_log)")]
    assert names == [
        "id", "time", "user_id", "client_id", "connection_id", "tool", "status",
        "duration_ms", "error_class",
    ]  # fmt: skip
    assert "ix_audit_log_time" in indexes
    command.downgrade(config, revision.down_revision)
    assert "audit_log" not in tables(dev_settings)


async def add_row(when: datetime, tool: str = "t") -> None:
    async with write_transaction() as db:
        db.add(AuditLog(time=when, user_id=1, client_id="c", tool=tool, status="ok", duration_ms=1))


async def test_record_writes_one_row(app: FastAPI) -> None:
    await audit.record(
        user_id=3,
        client_id="c" * 100,
        connection_id="x" * 32,
        tool="list_connections",
        status="denied",
        duration_ms=-5,
        error_class="writes_disabled",
    )
    async with session() as db:
        row = (await db.scalars(select(AuditLog))).one()
    assert (row.user_id, row.tool, row.status, row.error_class) == (
        3, "list_connections", "denied", "writes_disabled",
    )  # fmt: skip
    assert row.duration_ms == 0 and len(row.client_id) == 64
    assert abs(row.time - datetime.now(UTC)) < timedelta(seconds=30)


async def test_record_refuses_an_unknown_status(app: FastAPI) -> None:
    with pytest.raises(ValueError, match="unknown audit status"):
        await audit.record(
            user_id=1, client_id="c", connection_id=None, tool="t", status="weird", duration_ms=1
        )


async def test_purge_deletes_only_entries_older_than_the_retention_period(app: FastAPI) -> None:
    set_settings_override(Settings(environment="development", audit_retention_days=30))
    now = datetime.now(UTC)
    await add_row(now - timedelta(days=31), "old")
    await add_row(now - timedelta(days=29), "recent")
    await add_row(now, "new")
    assert await audit.purge_expired() == 1
    async with session() as db:
        left = sorted((await db.scalars(select(AuditLog.tool))).all())
    assert left == ["new", "recent"]
    assert await audit.purge_expired() == 0


async def test_the_default_retention_is_90_days(app: FastAPI) -> None:
    now = datetime.now(UTC)
    await add_row(now - timedelta(days=91), "old")
    await add_row(now - timedelta(days=89), "recent")
    assert await audit.purge_expired() == 1


async def test_the_cleanup_runs_at_startup_and_stops_cleanly(app: FastAPI) -> None:
    await add_row(datetime.now(UTC) - timedelta(days=400), "ancient")
    task = audit.start_purge_task()
    for _ in range(50):
        await asyncio.sleep(0.05)
        async with session() as db:
            if not (await db.scalars(select(AuditLog))).all():
                break
    async with session() as db:
        assert (await db.scalars(select(AuditLog))).all() == []
    await audit.stop_purge_task(task)
    assert task.done()
