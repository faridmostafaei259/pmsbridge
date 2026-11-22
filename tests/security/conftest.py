"""Fixtures for the security regression tests. Nothing here needs Docker or the network."""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


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
def key_entry() -> str:
    """A throwaway `<id>:<key>` entry, different on every run."""
    return generate_key("sec1")


@pytest.fixture
def secure_settings(tmp_path: Path, key_entry: str) -> Settings:
    """Development settings with a real key and a temporary data folder."""
    return Settings(
        environment="development",
        data_dir=tmp_path / "data",
        encryption_keys=key_entry,  # pyright: ignore[reportArgumentType]
    )
