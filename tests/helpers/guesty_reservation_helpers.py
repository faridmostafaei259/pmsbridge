"""Helpers for the Guesty reservation tests: invented fixtures, a stand-in token manager, a client.

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

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty" / "reservations"
BASE = "https://open-api.guesty.com/v1"
SEARCH_URL = f"{BASE}/reservations-v3/search"
BY_IDS_URL = f"{BASE}/reservations-v3"
REPORTS_URL = f"{BASE}/reservations-reports/reservations/search"
GUEST_URL = f"{BASE}/guests-crud"

RESERVATION_ID = "6a2000000000000000000001"
GUEST_ID = "6a2000000000000000000a01"
# Invented values that must never appear in a result or a log record.
DOOR_CODE = "FAKE-DOOR-4821"
STAFF_NAME = "Invented Staffmember"
GUEST_NAME = "Alex Invented"
GUEST_EMAIL = "alex.invented@example.test"
GUEST_PHONE = "+15550100"
GUEST_NOTE = "Arriving late, after nine."


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


class StandInTokens:
    """What `GuestyClient` needs from the token manager, without any token request."""

    account_id: str | None = "acct-reservation-tests"

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
    return GuestyClient(StandInTokens(), "conn-reservation-tests", http)  # type: ignore[arg-type]


def json_response(body: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def mock_router() -> respx.MockRouter:
    """Every outgoing request is intercepted. An unmocked address fails the test."""
    return respx.mock(assert_all_called=False)


def mock_detail(
    router: respx.MockRouter,
    *,
    by_ids: object | None = None,
    guest: httpx.Response | None = None,
    report: httpx.Response | None = None,
) -> dict[str, respx.Route]:
    """Mock the three calls behind get_reservation (every one succeeds unless overridden)."""
    return {
        "by_ids": router.get(BY_IDS_URL).mock(
            return_value=json_response(fixture("by_ids.json") if by_ids is None else by_ids)
        ),
        "guest": router.get(f"{GUEST_URL}/{GUEST_ID}").mock(
            return_value=guest or json_response(fixture("guest.json"))
        ),
        "report": router.get(REPORTS_URL).mock(
            return_value=report or json_response(fixture("report.json"))
        ),
    }
