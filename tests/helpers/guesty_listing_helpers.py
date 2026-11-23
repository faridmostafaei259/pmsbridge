"""Helpers for the Guesty listing tests: invented fixtures and the shared client helpers.

Nothing here talks to the real service (see `guesty_client_helpers`).
"""

import json
from pathlib import Path

from guesty_client_helpers import BASE, StandInTokens, json_response, make_client, mock_router

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty" / "listings"
LISTINGS_URL = f"{BASE}/listings"
PHOTOS_URL = f"{BASE}/properties-api/property-photos/property-photos"

__all__ = [
    "BASE",
    "LISTINGS_URL",
    "PHOTOS_URL",
    "StandInTokens",
    "fixture",
    "json_response",
    "make_client",
    "mock_router",
]


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())
