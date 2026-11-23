"""Clean shared client state around each test."""

from collections.abc import Iterator

import pytest

from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.pms.guesty import client as guesty_client


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    clear_settings_cache()
    guesty_client.reset_throttles()
    yield
    guesty_client.reset_throttles()
