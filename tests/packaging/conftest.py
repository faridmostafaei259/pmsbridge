"""Fixtures for the installer tests."""

from pathlib import Path

import pytest
from packaging_helpers import Sandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)
