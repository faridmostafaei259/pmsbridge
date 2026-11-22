"""Shared test configuration.

Every test runs in development mode with all settings isolated: the data directory is under a
temporary path, no setting is inherited from the developer's environment, and the shared settings
override and database are reset afterwards. Nothing reads host settings or writes to /data.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from pmsbridge.core import storage
from pmsbridge.core.settings import clear_settings_cache, set_settings_override

_INHERITED_SETTINGS = (
    "DATABASE_PATH",
    "BACKUP_DIR",
    "PUBLIC_BASE_URL",
    "DOMAIN",
    "PRE_MIGRATION_BACKUPS_KEEP",
    "LOG_LEVEL",
    "TRUSTED_PROXIES",
)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for name in _INHERITED_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    set_settings_override(None)
    clear_settings_cache()
    yield
    set_settings_override(None)
    clear_settings_cache()
    if storage._database is not None:  # test isolation needs the shared handle
        asyncio.run(storage.reset_database())
