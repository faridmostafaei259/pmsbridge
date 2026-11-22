"""Alembic environment: asynchronous, SQLite batch mode, metadata from every feature's models.

The database file comes from the Alembic configuration (`config.attributes["database_path"]`, set by
`pmsbridge.core.storage.migrate`) or, when Alembic is run by hand from the repository, from the
application settings (`DATA_DIR` or `DATABASE_PATH`).

Foreign key enforcement is off while migrating, because SQLite rebuilds a table to change it and
that is not allowed with enforcement on. When the migrations are done, the keys are checked and the
upgrade is rolled back if any row breaks one.
"""

import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection

from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import Base, StorageError, build_engine, import_model_modules

config = context.config
if config.config_file_name is not None:
    # Only when run by hand with alembic.ini. The application builds its configuration in code.
    fileConfig(config.config_file_name, disable_existing_loggers=False)
import_model_modules()
target_metadata = Base.metadata


def _database_path() -> Path:
    configured = config.attributes.get("database_path")
    if isinstance(configured, Path):
        return configured
    return get_settings().database_path


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        transactional_ddl=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
        broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise StorageError(
                f"the migration left {len(broken)} row(s) that break a foreign key; "
                "nothing was changed"
            )


async def _run_online() -> None:
    engine = build_engine(_database_path(), foreign_keys=False)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


def _run_offline() -> None:
    context.configure(
        url=f"sqlite:///{_database_path()}",
        target_metadata=target_metadata,
        render_as_batch=True,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    asyncio.run(_run_online())
