"""Every authorization test starts with full rate-limit buckets (grant ids repeat across tests)."""

import pytest

from pmsbridge.mcp.framework import reset_rate_limits


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> None:
    reset_rate_limits()
