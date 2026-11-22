import asyncio
import logging
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from pmsbridge.core import storage
from pmsbridge.core.logging import configure_logging
from pmsbridge.core.settings import Settings, override_settings
from pmsbridge.core.storage import (
    Database,
    StorageError,
    backup_database_file,
    migrate,
    reset_database,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def scalar(database: Database, sql: str) -> object:
    async with database.session() as session:
        return (await session.execute(text(sql))).scalar()


# ---- Pragmas, transactions, concurrency ----


async def test_every_connection_has_the_pragmas(database: Database) -> None:
    assert await scalar(database, "PRAGMA journal_mode") == "wal"
    assert await scalar(database, "PRAGMA busy_timeout") == 5000
    assert await scalar(database, "PRAGMA foreign_keys") == 1
    assert await scalar(database, "PRAGMA synchronous") == 1  # NORMAL


async def test_pragmas_hold_on_a_second_pooled_connection(database: Database) -> None:
    async with database.session() as first, database.session() as second:
        await first.execute(text("SELECT 1"))
        await second.execute(text("SELECT 1"))
        for session in (first, second):
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            assert (await session.execute(text("PRAGMA busy_timeout"))).scalar() == 5000


async def test_foreign_keys_are_enforced(database: Database) -> None:
    async with database.write_transaction() as session:
        await session.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
        await session.execute(
            text(
                "CREATE TABLE child "
                "(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
            )
        )
    with pytest.raises(Exception, match="FOREIGN KEY"):
        async with database.write_transaction() as session:
            await session.execute(text("INSERT INTO child (parent_id) VALUES (99)"))


async def test_write_transaction_commits_and_rolls_back(database: Database) -> None:
    async with database.write_transaction() as session:
        await session.execute(text("CREATE TABLE t (n INTEGER)"))
        await session.execute(text("INSERT INTO t VALUES (1)"))
    with pytest.raises(RuntimeError):
        async with database.write_transaction() as session:
            await session.execute(text("INSERT INTO t VALUES (2)"))
            raise RuntimeError("abort")
    assert await scalar(database, "SELECT count(*) FROM t") == 1


async def test_write_transaction_starts_with_begin_immediate(database: Database) -> None:
    statements: list[str] = []

    @event.listens_for(database.engine.sync_engine, "before_cursor_execute")
    def record(*args: object) -> None:  # pyright: ignore[reportUnusedFunction]
        statements.append(str(args[2]))

    async with database.write_transaction() as session:
        await session.execute(text("SELECT 1"))
    async with database.session() as session:
        await session.execute(text("SELECT 1"))
    begins = [s for s in statements if s.startswith("BEGIN")]
    assert begins == ["BEGIN IMMEDIATE", "BEGIN DEFERRED"]


async def test_a_second_writer_waits_instead_of_failing(database: Database) -> None:
    async with database.write_transaction() as session:
        await session.execute(text("CREATE TABLE t (n INTEGER)"))
    release = asyncio.Event()
    holding = asyncio.Event()

    async def holder() -> None:
        async with database.write_transaction() as session:
            await session.execute(text("INSERT INTO t VALUES (1)"))
            holding.set()
            await release.wait()

    async def waiter() -> None:
        await holding.wait()
        async with database.write_transaction() as session:
            await session.execute(text("INSERT INTO t VALUES (2)"))

    first = asyncio.create_task(holder())
    second = asyncio.create_task(waiter())
    await holding.wait()
    await asyncio.sleep(0.3)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert await scalar(database, "SELECT count(*) FROM t") == 2


async def test_twenty_concurrent_writers_never_hit_database_is_locked(database: Database) -> None:
    async with database.write_transaction() as session:
        await session.execute(text("CREATE TABLE counter (n INTEGER)"))
        await session.execute(text("INSERT INTO counter VALUES (0)"))

    async def bump() -> None:
        async with database.write_transaction() as session:
            current = (await session.execute(text("SELECT n FROM counter"))).scalar_one()
            await asyncio.sleep(0.005)
            await session.execute(text("UPDATE counter SET n = :n"), {"n": current + 1})

    results = await asyncio.gather(*(bump() for _ in range(20)), return_exceptions=True)
    assert [r for r in results if isinstance(r, BaseException)] == []
    assert await scalar(database, "SELECT n FROM counter") == 20


async def test_integrity_check_reports_a_healthy_database(database: Database) -> None:
    assert await database.integrity_check() == []


async def test_integrity_check_reports_corruption(tmp_path: Path) -> None:
    path = tmp_path / "damaged.db"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA journal_mode=DELETE")
    raw.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    raw.execute("CREATE INDEX t_b ON t (b)")
    raw.executemany("INSERT INTO t VALUES (?, ?)", [(n, f"value-{n}") for n in range(2000)])
    raw.commit()
    raw.close()
    with path.open("r+b") as handle:
        handle.seek(4096 * 3)
        handle.write(os.urandom(4096))
    database = Database(path)
    try:
        problems = await database.integrity_check()
        assert problems
    finally:
        await database.dispose()


# ---- Files and permissions ----


async def test_database_and_directories_are_private(dev_settings: Settings) -> None:
    migrate(dev_settings)
    database = Database(dev_settings.database_path)
    await database.ping()
    try:
        assert mode(dev_settings.data_dir) == 0o700
        assert mode(dev_settings.backup_dir) == 0o700
        assert mode(dev_settings.database_path) == 0o600
        for sidecar in dev_settings.database_path.parent.glob("pmsbridge.db-*"):
            assert mode(sidecar) == 0o600
    finally:
        await database.dispose()


def test_loose_permissions_are_tightened(dev_settings: Settings) -> None:
    dev_settings.data_dir.mkdir(parents=True)
    dev_settings.data_dir.chmod(0o755)
    dev_settings.database_path.write_bytes(b"")
    dev_settings.database_path.chmod(0o644)
    migrate(dev_settings)
    assert mode(dev_settings.data_dir) == 0o700
    assert mode(dev_settings.database_path) == 0o600


# ---- Online backup ----


async def test_online_backup_is_valid_while_writes_continue(
    database: Database, tmp_path: Path
) -> None:
    async with database.write_transaction() as session:
        await session.execute(text("CREATE TABLE t (n INTEGER, pad TEXT)"))
    stop = asyncio.Event()
    written = 0

    async def writer() -> None:
        nonlocal written
        while not stop.is_set():
            async with database.write_transaction() as session:
                await session.execute(
                    text("INSERT INTO t VALUES (:n, :pad)"), {"n": written, "pad": "x" * 500}
                )
            written += 1

    task = asyncio.create_task(writer())
    await asyncio.sleep(0.2)
    before = written
    destination = tmp_path / "backups" / "copy.db"
    await database.online_backup(destination)
    after = written
    stop.set()
    await task

    assert mode(destination) == 0o600
    assert not destination.with_name("copy.db.partial").exists()
    copy = sqlite3.connect(destination)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        rows = copy.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        copy.close()
    assert before <= rows <= after + 1
    assert [p.name for p in destination.parent.iterdir()] == ["copy.db"]


def test_backup_of_a_missing_database_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="does not exist"):
        backup_database_file(tmp_path / "nope.db", tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()


def test_backup_of_a_damaged_database_leaves_no_file(tmp_path: Path) -> None:
    source = tmp_path / "bad.db"
    source.write_bytes(b"this is not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        backup_database_file(source, tmp_path / "out.db")
    assert list(tmp_path.glob("out.db*")) == []


def test_existing_parent_folders_keep_their_permissions(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    settings = Settings(
        environment="development",
        data_dir=tmp_path / "data",
        database_path=shared / "created" / "app.db",
    )
    migrate(settings)
    assert mode(shared) == 0o755
    assert mode(shared / "created") == 0o700
    assert mode(settings.database_path) == 0o600


# ---- Error messages never carry bound values ----


async def test_bound_values_stay_out_of_errors_and_logs(
    database: Database, dev_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "distinctive-value-7f3a91"  # noqa: S105
    async with database.write_transaction() as session:
        await session.execute(text("CREATE TABLE people (email TEXT UNIQUE)"))
        await session.execute(text("INSERT INTO people VALUES (:v)"), {"v": secret})
    caught: IntegrityError | None = None
    try:
        async with database.write_transaction() as session:
            await session.execute(text("INSERT INTO people VALUES (:v)"), {"v": secret})
    except IntegrityError as exc:
        caught = exc
    assert caught is not None
    assert secret not in str(caught)
    assert secret not in repr(caught)

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        configure_logging(dev_settings)
        logging.getLogger("pmsbridge.test").error("write failed", exc_info=caught)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
    assert secret not in capsys.readouterr().out


def test_shared_database_follows_the_configured_path(dev_settings: Settings) -> None:
    async def run() -> None:
        with override_settings(dev_settings):
            migrate(dev_settings)
            async with storage.write_transaction() as session:
                await session.execute(text("CREATE TABLE shared (n INTEGER)"))
            async with storage.session() as session:
                assert (await session.execute(text("SELECT count(*) FROM shared"))).scalar() == 0
            assert storage.get_database() is storage.get_database()
        await reset_database()

    asyncio.run(run())
