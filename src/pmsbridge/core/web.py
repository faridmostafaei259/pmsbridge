"""Storage feature: runs the database migrations at startup and adds the database readiness check.

`register(app)` is called by the application factory before the server accepts requests. If the
database cannot be prepared or migrated, it raises a `StorageError` whose message tells the
operator what state the database is in and what to do. The entry point logs that error once and
exits; this module does not log it again. An invalid configuration raises `SettingsError`, which
the entry point reports before the app is built.
"""

import logging

from fastapi import FastAPI

from pmsbridge.app import ReadinessProbe, add_readiness_check
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import Database, get_database, migrate

logger = logging.getLogger(__name__)


def _database_probe(database: Database) -> ReadinessProbe:
    async def probe() -> tuple[bool, str]:
        try:
            await database.ping()
        except Exception as exc:  # noqa: BLE001  (any failure means "not ready", reported by name)
            return False, f"database query failed: {type(exc).__name__}: {exc}"
        return True, "ok"

    return probe


def register(app: FastAPI) -> None:
    settings = get_settings()
    result = migrate(settings)
    logger.info(
        "database ready",
        extra={
            "database": str(settings.database_path),
            "backup_taken": result.backup_path is not None,
        },
    )
    database = get_database()
    app.state.database = database
    add_readiness_check(app, "database", _database_probe(database))
