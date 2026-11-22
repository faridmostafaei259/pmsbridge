"""SQLite storage: engine, sessions, write transactions, backups and migrations.

One SQLite file in WAL mode holds all persistent state (see ADR 0003). Every connection is set up
with `busy_timeout=5000`, `foreign_keys=ON`, `journal_mode=WAL` and `synchronous=NORMAL`.

How to use it from a feature module:

* Reads: `async with session() as s: ...`.
* Writes: always `async with write_transaction() as s: ...`. It starts the transaction with
  `BEGIN IMMEDIATE`, so writers queue up on the database lock (waiting up to the busy timeout)
  instead of failing halfway with "database is locked". A plain `session()` starts a deferred
  transaction, and a deferred transaction that turns into a write can fail with "database is locked"
  without waiting. Do not write through `session()`.
* Models: subclass `Base` in a module named `<feature>.db` listed in `MODEL_MODULES`, and add a
  migration under `migrations/versions/`.

Migrations live inside the package (`pmsbridge/migrations`) because the container image ships only
`src/`. The top-level `migrations` entry in the repository is a link to that folder, so the usual
`migrations/versions/` path and `alembic.ini` work for developers.
"""

import asyncio
import contextlib
import importlib
import logging
import os
import re
import sqlite3
import stat
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util.exc import CommandError
from sqlalchemy import MetaData, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import ConnectionPoolEntry

from pmsbridge.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS: Final = 5000
PRIVATE_FILE_MODE: Final = 0o600
PRIVATE_DIR_MODE: Final = 0o700
PRE_MIGRATION_PREFIX: Final = "pre-migration-"
MIGRATIONS_DIR: Final = Path(__file__).resolve().parent.parent / "migrations"

# The execution option that tells the "begin" hook which kind of transaction to start.
BEGIN_MODE_OPTION: Final = "pmsbridge_begin_mode"
BeginMode = Literal["DEFERRED", "IMMEDIATE"]

# Modules that define database models. Each is imported by `import_model_modules()`, which the
# migration environment calls so that `Base.metadata` is complete. A module that does not exist yet
# is skipped, as with the feature modules in `pmsbridge.app`.
MODEL_MODULES: Final[tuple[str, ...]] = (
    "pmsbridge.core.db",
    "pmsbridge.accounts.db",
    "pmsbridge.oauth.db",
    "pmsbridge.pms.db",
    "pmsbridge.mcp.db",
)

# Named constraints let Alembic's batch mode (which rebuilds tables on SQLite) find and change them.
NAMING_CONVENTION: Final = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every feature's models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class StorageError(RuntimeError):
    """Storage could not be prepared, backed up or migrated."""


# ---- Files and permissions ----


def ensure_private_dir(path: Path, *, restrict_existing: bool = False) -> None:
    """Create a directory (and missing parents) readable only by the current user.

    Only directories created here get the private mode. An existing `path` is left as it is unless
    `restrict_existing` is true, and existing parents are never touched.
    """
    missing: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=True)
        _restrict(directory, PRIVATE_DIR_MODE)
    if restrict_existing and path not in missing:
        _restrict(path, PRIVATE_DIR_MODE)


def ensure_private_file(path: Path) -> None:
    """Create an empty file if it is missing and make it readable only by the current user."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, PRIVATE_FILE_MODE)
    os.close(fd)
    _restrict(path, PRIVATE_FILE_MODE)


def _restrict(path: Path, mode: int) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) != mode:
            path.chmod(mode)
    except PermissionError:
        # For example a folder owned by another user that we are only allowed to write to.
        logger.warning("could not set permissions %o on %s", mode, path)


def prepare_storage(settings: Settings) -> None:
    """Create the data folder, the backup folder and the database file with private permissions.

    The data and backup folders are restricted even when they already exist. Other folders on the
    way to `DATABASE_PATH` are only created, never changed.
    """
    ensure_private_dir(settings.data_dir, restrict_existing=True)
    ensure_private_dir(settings.backup_dir, restrict_existing=True)
    ensure_private_file(settings.database_path)


# ---- Engine, sessions and transactions ----


def build_engine(database_path: Path, *, foreign_keys: bool = True) -> AsyncEngine:
    """Create an async engine whose connections all use the pragmas described in the module doc.

    `foreign_keys=False` is for migrations only: SQLite rebuilds tables in batch mode, which needs
    foreign key enforcement off (the migration environment checks the keys afterwards instead).
    """
    # hide_parameters keeps bound values (which can be secrets or guest data) out of error messages.
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", hide_parameters=True)
    sync_engine = engine.sync_engine

    @event.listens_for(sync_engine, "connect")
    def _on_connect(  # pyright: ignore[reportUnusedFunction]
        dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
    ) -> None:
        # Turn off the driver's own transaction handling so that "begin" below controls it.
        dbapi_connection.isolation_level = None  # pyright: ignore[reportAttributeAccessIssue]
        cursor = dbapi_connection.cursor()
        try:
            # The busy timeout comes first so that the other pragmas also wait for a lock.
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        finally:
            cursor.close()

    @event.listens_for(sync_engine, "begin")
    def _on_begin(connection: Connection) -> None:  # pyright: ignore[reportUnusedFunction]
        mode = connection.get_execution_options().get(BEGIN_MODE_OPTION, "DEFERRED")
        if mode not in ("DEFERRED", "IMMEDIATE"):
            raise StorageError(f"unsupported transaction mode {mode!r}")
        connection.exec_driver_sql(f"BEGIN {mode}")

    return engine


class Database:
    """The engine and session factory for one database file."""

    def __init__(self, path: Path) -> None:
        ensure_private_file(path)
        self.path = path
        self.engine = build_engine(path)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        """A session for reading. Use it as `async with database.session() as s:`."""
        return self._sessions()

    @asynccontextmanager
    async def write_transaction(self) -> AsyncGenerator[AsyncSession]:
        """A session inside a transaction that starts with `BEGIN IMMEDIATE`.

        Commits when the block ends normally and rolls back when it raises.
        """
        async with self._sessions() as session, session.begin():
            # The first use of the connection starts the transaction in IMMEDIATE mode.
            await session.connection(execution_options={BEGIN_MODE_OPTION: "IMMEDIATE"})
            yield session

    async def ping(self) -> None:
        """Run a trivial query that reads the database file. Raises if it cannot be used.

        `SELECT 1` would not do: it never opens the file, so a damaged file would pass.
        """
        async with self.session() as session:
            await session.execute(text("SELECT count(*) FROM sqlite_master"))

    async def integrity_check(self) -> list[str]:
        """Run SQLite's integrity check. Returns the problems found; an empty list means healthy."""
        try:
            async with self.session() as session:
                rows = (await session.execute(text("PRAGMA integrity_check"))).scalars().all()
        except DatabaseError as exc:
            # SQLite sometimes cannot even run the check on a badly damaged file.
            return [str(exc.orig)]
        return [str(row) for row in rows if str(row) != "ok"]

    async def online_backup(self, destination: Path) -> Path:
        """Write a consistent copy of the live database to `destination` (not a file copy)."""
        await asyncio.to_thread(backup_database_file, self.path, destination)
        return destination

    async def dispose(self) -> None:
        await self.engine.dispose()


_database: Database | None = None


def get_database() -> Database:
    """The shared database for the configured `DATABASE_PATH`, created on first use."""
    global _database  # noqa: PLW0603
    if _database is None:
        _database = Database(get_settings().database_path)
    return _database


async def reset_database() -> None:
    """Close the shared database so the next `get_database()` builds a new one (tests)."""
    global _database
    database, _database = _database, None
    if database is not None:
        await database.dispose()


def session() -> AsyncSession:
    """A read session on the shared database."""
    return get_database().session()


def write_transaction() -> contextlib.AbstractAsyncContextManager[AsyncSession]:
    """A `BEGIN IMMEDIATE` transaction on the shared database. See `Database.write_transaction`."""
    return get_database().write_transaction()


# ---- Backups ----


def backup_database_file(source: Path, destination: Path) -> None:
    """Copy a live SQLite database with SQLite's online backup API.

    The copy is taken from a consistent snapshot while other connections keep writing. It is written
    to a temporary file, checked with `integrity_check`, and moved into place, so a failed backup
    never leaves a half-written file under the final name. The result is a single file
    (journal mode DELETE) with private permissions.
    """
    if not source.is_file():
        raise StorageError(f"cannot back up {source}: the database file does not exist")
    ensure_private_dir(destination.parent)
    partial = destination.with_name(destination.name + ".partial")
    partial.unlink(missing_ok=True)
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    os.close(fd)
    try:
        origin = sqlite3.connect(source, timeout=BUSY_TIMEOUT_MS / 1000)
        copy = sqlite3.connect(partial)
        try:
            origin.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
            problems = [str(row[0]) for row in copy.execute("PRAGMA integrity_check")]
        finally:
            copy.close()
            origin.close()
        if problems != ["ok"]:
            raise StorageError("the backup failed its integrity check and was discarded")
        partial.chmod(PRIVATE_FILE_MODE)
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


# ---- Models and migrations ----


def import_model_modules(modules: tuple[str, ...] = MODEL_MODULES) -> list[str]:
    """Import every feature's model module so that `Base.metadata` knows all tables.

    Returns the names of the modules that were imported. Only a missing model module is skipped.
    """
    imported: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            continue
        imported.append(name)
    return imported


@dataclass(frozen=True)
class MigrationResult:
    """What `migrate()` did."""

    backup_path: Path | None
    revisions_before: tuple[str, ...]
    revisions_after: tuple[str, ...]


def alembic_config(database_path: Path, script_location: Path = MIGRATIONS_DIR) -> Config:
    """Alembic configuration built in code, so it works inside the installed package."""
    config = Config()
    config.set_main_option("script_location", str(script_location).replace("%", "%%"))
    config.attributes["database_path"] = database_path
    return config


def _schema_exists(path: Path) -> bool:
    with contextlib.closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)) as connection:
        row = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()
    return bool(row and row[0])


def _current_revisions(path: Path) -> tuple[str, ...]:
    with contextlib.closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)) as connection:
        has_table = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'alembic_version'"
        ).fetchone()
        if not has_table or not has_table[0]:
            return ()
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    return tuple(sorted(str(row[0]) for row in rows))


BACKUP_NAME = re.compile(r"^pre-migration-(\d{8}T\d{6}Z)-(\d+)-from-(.+)\.db$")

NEWER_VERSION_MESSAGE = (
    "The database was created by a newer version of pmsbridge: it is at revision {revisions}, "
    "which this version does not know. The database was not changed. Go back to the newer "
    "version, or restore a pre-migration backup taken before the newer version ran (they are in "
    "{backup_dir}). See 'If the application stops after an update' in docs/configuration.md."
)
UPGRADE_FAILED_MESSAGE = (
    "The database upgrade failed ({error}) and was rolled back, so the database was left "
    "unchanged.{backup} The application cannot start until this is fixed: go back to the previous "
    "version, or restore the backup. See 'If the application stops after an update' in "
    "docs/configuration.md."
)


def _origin(revisions: tuple[str, ...]) -> str:
    return "+".join(revisions) if revisions else "unversioned"


def _backup_path(backup_dir: Path, revisions: tuple[str, ...]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    taken = [
        int(match[2])
        for existing in backup_dir.glob(f"{PRE_MIGRATION_PREFIX}{stamp}-*.db")
        if (match := BACKUP_NAME.match(existing.name))
    ]
    counter = max(taken, default=0) + 1
    return backup_dir / f"{PRE_MIGRATION_PREFIX}{stamp}-{counter}-from-{_origin(revisions)}.db"


def _list_backups(backup_dir: Path) -> list[tuple[Path, str]]:
    """Pre-migration backups, oldest first, with the revisions they were taken from.

    Sorted by (timestamp, counter). Files that do not follow the naming scheme are ignored, so they
    are never pruned.
    """
    found: list[tuple[str, int, Path, str]] = []
    for path in backup_dir.glob(f"{PRE_MIGRATION_PREFIX}*.db"):
        if match := BACKUP_NAME.match(path.name):
            found.append((match[1], int(match[2]), path, match[3]))
    found.sort(key=lambda item: (item[0], item[1]))
    return [(path, origin) for _, _, path, origin in found]


def prune_pre_migration_backups(
    backup_dir: Path, keep: int, protect: Path | None = None
) -> list[Path]:
    """Delete the oldest pre-migration backups so that at most `keep` remain.

    `protect` (the backup that was just taken) is never deleted and counts towards `keep`.
    Returns the deleted files.
    """
    others = [path for path, _ in _list_backups(backup_dir) if path != protect]
    room = max(0, keep - (1 if protect is not None else 0))
    removed = others[: max(0, len(others) - room)]
    for old in removed:
        old.unlink(missing_ok=True)
    return removed


def _unchanged_since(database_path: Path, backup: Path) -> bool:
    """True when neither the database nor its write-ahead log changed after `backup` was written.

    A rolled-back migration leaves the database file alone, so repeated failing restarts do not
    take a new backup each time. A write-ahead log that still holds data counts as a change.
    """
    cutoff = backup.stat().st_mtime_ns
    wal = database_path.with_name(database_path.name + "-wal")
    if database_path.stat().st_mtime_ns > cutoff:
        return False
    return not (wal.exists() and (wal.stat().st_size > 0 or wal.stat().st_mtime_ns > cutoff))


def _run_blocking[T](function: Callable[[], T]) -> T:
    """Run `function` in this thread, or in a helper thread when an event loop is running here.

    Alembic's async environment starts its own event loop, which cannot happen inside a running one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return function()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(function).result()


def _unknown_revisions(script: ScriptDirectory, revisions: tuple[str, ...]) -> list[str]:
    unknown: list[str] = []
    for revision in revisions:
        try:
            script.get_revision(revision)
        except (CommandError, RevisionError):
            unknown.append(revision)
    return unknown


def _take_backup(settings: Settings, before: tuple[str, ...]) -> Path:
    """Back up the database before an upgrade, or reuse the newest backup if it is still current."""
    backups = _list_backups(settings.backup_dir)
    if backups:
        newest, origin = backups[-1]
        if origin == _origin(before) and _unchanged_since(settings.database_path, newest):
            logger.info("reusing the existing pre-migration backup", extra={"backup": newest.name})
            return newest
    backup = _backup_path(settings.backup_dir, before)
    backup_database_file(settings.database_path, backup)
    pruned = prune_pre_migration_backups(
        settings.backup_dir, settings.pre_migration_backups_keep, protect=backup
    )
    logger.info(
        "backed up the database before migrating",
        extra={"backup": backup.name, "pruned_backups": len(pruned)},
    )
    return backup


def migrate(
    settings: Settings | None = None, *, script_location: Path = MIGRATIONS_DIR
) -> MigrationResult:
    """Bring the database up to the newest revision.

    When the database already holds data and a migration is pending, a backup is taken first with
    the online backup API (or the newest backup is reused when nothing changed since), and old
    pre-migration backups are pruned to `PRE_MIGRATION_BACKUPS_KEEP`. A fresh database and an up to
    date database take no backup. Every failure raises a `StorageError` whose message tells the
    operator what state the database is in and what to do. `script_location` is for tests.
    """
    settings = settings or get_settings()
    try:
        prepare_storage(settings)
        config = alembic_config(settings.database_path, script_location)
        script = ScriptDirectory.from_config(config)
        heads = tuple(sorted(script.get_heads()))
        before = _current_revisions(settings.database_path)
        has_schema = _schema_exists(settings.database_path)
    except (OSError, sqlite3.Error) as exc:
        raise StorageError(
            f"The database file {settings.database_path} cannot be opened ({type(exc).__name__}). "
            "The database was not changed. Check the file's owner and permissions, and that it is "
            "an SQLite database."
        ) from exc

    unknown = _unknown_revisions(script, before)
    if unknown:
        raise StorageError(
            NEWER_VERSION_MESSAGE.format(
                revisions=", ".join(unknown), backup_dir=settings.backup_dir
            )
        )

    backup_path: Path | None = None
    if before != heads:
        if has_schema:
            try:
                backup_path = _take_backup(settings, before)
            except (OSError, sqlite3.Error, StorageError) as exc:
                raise StorageError(
                    f"The backup before the upgrade failed ({type(exc).__name__}), so nothing was "
                    "upgraded and the database was not changed."
                ) from exc
        try:
            _run_blocking(lambda: command.upgrade(config, "head"))
        except Exception as exc:
            where = (
                f" A backup from just before the upgrade is at {backup_path}."
                if backup_path
                else ""
            )
            raise StorageError(
                UPGRADE_FAILED_MESSAGE.format(error=type(exc).__name__, backup=where)
            ) from exc

    after = _current_revisions(settings.database_path)
    logger.info(
        "database is up to date",
        extra={"revisions_before": list(before), "revisions_after": list(after)},
    )
    return MigrationResult(backup_path, before, after)
