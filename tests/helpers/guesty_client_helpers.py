"""Shared pieces of the Guesty adapter tests: a stand-in token manager, a client, a respx router.

Nothing here talks to the real service. The only network is a respx router, and the token manager
is replaced so no token is ever requested. The listing, reservation and calendar helper modules
re-export these names.
"""

import httpx
import respx

from pmsbridge.pms.base import Provider
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.http import PMSHttpClient

BASE = "https://open-api.guesty.com/v1"

__all__ = ["BASE", "StandInTokens", "json_response", "make_client", "mock_router"]


class StandInTokens:
    """What `GuestyClient` needs from the token manager, without any token request."""

    account_id: str | None = "acct-guesty-tests"

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
    return GuestyClient(StandInTokens(), "conn-guesty-tests", http)  # type: ignore[arg-type]


def json_response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def mock_router() -> respx.MockRouter:
    """Every outgoing request is intercepted. An unmocked address fails the test."""
    return respx.mock(assert_all_called=False)
