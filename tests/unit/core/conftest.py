"""Shared fixtures for the core tests. Nothing here touches /data or the real environment."""

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from pmsbridge.core import crypto
from pmsbridge.core.settings import Settings, clear_settings_cache, set_settings_override
from pmsbridge.core.storage import Database, migrate, reset_database

SETTING_VARIABLES = (
    "ENVIRONMENT",
    "PUBLIC_BASE_URL",
    "DOMAIN",
    "DATA_DIR",
    "DATABASE_PATH",
    "BACKUP_DIR",
    "PRE_MIGRATION_BACKUPS_KEEP",
    "LOG_LEVEL",
    "ENCRYPTION_KEYS",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start every test without setting variables and with no cached or overridden settings."""
    for name in SETTING_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    clear_settings_cache()
    set_settings_override(None)
    yield
    clear_settings_cache()
    set_settings_override(None)


@pytest.fixture(autouse=True)
def isolated_encrypted_field_registry() -> Iterator[None]:
    """Feature modules register their own columns when imported; these tests see only their own."""
    saved = dict(crypto._registry)  # pyright: ignore[reportPrivateUsage]
    crypto._registry.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    crypto._registry.clear()  # pyright: ignore[reportPrivateUsage]
    crypto._registry.update(saved)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put the root logger back as it was after a test that reconfigures it."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


@pytest.fixture
def dev_settings(tmp_path: Path) -> Settings:
    """Development settings whose data folder is inside the test's temporary directory."""
    return Settings(environment="development", data_dir=tmp_path / "data")


@pytest.fixture
async def database(dev_settings: Settings) -> AsyncIterator[Database]:
    """A database with the schema created by the real migrations, in a temporary directory."""
    migrate(dev_settings)
    db = Database(dev_settings.database_path)
    yield db
    await db.dispose()
    await reset_database()
