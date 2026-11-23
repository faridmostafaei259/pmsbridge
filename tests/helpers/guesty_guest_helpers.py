"""Helpers for the Guesty guest tests: invented fixtures and the shared client helpers.

Nothing here talks to the real service (see `guesty_client_helpers`).
"""

import json
from pathlib import Path

from guesty_client_helpers import BASE, StandInTokens, json_response, make_client, mock_router

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty" / "guests"
GUESTS_URL = f"{BASE}/guests-crud"

GUEST_ID = "6a3000000000000000000a01"
# Invented values. The first group may appear in results; the second must never be requested,
# returned or logged.
GUEST_NAME = "Alex Invented"
GUEST_EMAIL = "alex.invented@example.test"
GUEST_PHONE = "+15550100"
OTHER_NAME = "Sam Madeup"
OTHER_EMAIL = "sam.madeup@example.test"
NEVER_SHOWN = (
    "PASS-FAKE-777",
    "ID-FAKE-888",
    "1990-01-02",
    "1 Invented Street",
    "Staff-only note",
    "alex.second@example.test",
    "+15550199",
)
NEVER_REQUESTED = (
    "passportNumber",
    "identityNumber",
    "birthday",
    "address",
    "notes",
    "emails",
    "phones",
    "paymentMethods",
    "payment",
)

__all__ = [
    "BASE",
    "GUESTS_URL",
    "GUEST_EMAIL",
    "GUEST_ID",
    "GUEST_NAME",
    "GUEST_PHONE",
    "NEVER_REQUESTED",
    "NEVER_SHOWN",
    "OTHER_EMAIL",
    "OTHER_NAME",
    "StandInTokens",
    "fixture",
    "json_response",
    "make_client",
    "mock_router",
]


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())
