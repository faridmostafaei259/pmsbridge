"""Helpers for the Guesty reservation tests: invented fixtures and the shared client helpers.

Nothing here talks to the real service (see `guesty_client_helpers`).
"""

import json
from pathlib import Path

import httpx
import respx
from guesty_client_helpers import BASE, StandInTokens, json_response, make_client, mock_router

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty" / "reservations"
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

__all__ = [
    "BASE",
    "BY_IDS_URL",
    "DOOR_CODE",
    "GUEST_EMAIL",
    "GUEST_ID",
    "GUEST_NAME",
    "GUEST_NOTE",
    "GUEST_PHONE",
    "GUEST_URL",
    "REPORTS_URL",
    "RESERVATION_ID",
    "SEARCH_URL",
    "STAFF_NAME",
    "StandInTokens",
    "fixture",
    "json_response",
    "make_client",
    "mock_detail",
    "mock_router",
]


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


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
