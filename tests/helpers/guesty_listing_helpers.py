"""Helpers for the Guesty listing tests: invented fixtures, a stand-in token manager, and a client.

Nothing here talks to the real service. The only network is a respx router, and the token manager
is replaced so no token is ever requested.
"""

import json
from pathlib import Path

import httpx
import respx

from pmsbridge.pms.base import Provider
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.http import PMSHttpClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty" / "listings"
BASE = "https://open-api.guesty.com/v1"
LISTINGS_URL = f"{BASE}/listings"
PHOTOS_URL = f"{BASE}/properties-api/property-photos/property-photos"


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


class StandInTokens:
    """What `GuestyClient` needs from the token manager, without any token request."""

    account_id: str | None = "acct-listing-tests"

    async def get_token(self) -> str:
        return "fake-access-token"

    async def touch_success(self) -> None:
        return None

    async def refresh_after_rejection(self, rejected: str) -> str | None:
        return None

    async def mark_needs_attention(self) -> None:
        return None


def make_client() -> GuestyClient:
    http = PMSHttpClient(Provider.GUESTY, BASE)
    return GuestyClient(StandInTokens(), "conn-listing-tests", http)  # type: ignore[arg-type]


def json_response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def mock_router() -> respx.MockRouter:
    """Every outgoing request is intercepted. An unmocked address fails the test."""
    return respx.mock(assert_all_called=False)
