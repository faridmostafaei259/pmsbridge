"""Helpers for the Guesty owner and payment tests: invented fixtures and the shared client helpers.

Nothing here talks to the real service (see `guesty_client_helpers`).
"""

import json
from pathlib import Path

from guesty_client_helpers import BASE, StandInTokens, json_response, make_client, mock_router

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty"
OWNERS_URL = f"{BASE}/owners"
PAYMENTS_URL = f"{BASE}/payments"

OWNER_ID = "6a5000000000000000000b01"
OWNER_NAME = "Robin Madeup"
OWNER_EMAIL = "robin.madeup@example.test"
OWNER_PHONE = "+15550301"
RESERVATION_ID = "6a4000000000000000000e01"

# Invented values that must never be requested, returned or logged: owner payout and tax details.
OWNER_NEVER_SHOWN = (
    "FAKE-TAX-ID-55",
    "FAKE-IBAN-GB00-0000",
    "FAKE-ROUTING-0001",
    "FAKE-ACCOUNT-123456",
    "411199",
    "9 Invented Lane",
    "1234.5",
    "acct-fake-owner-1",
)
OWNER_NEVER_REQUESTED = (
    "taxId",
    "payoutMethods",
    "bankAccount",
    "address",
    "personalAddress",
    "workingCapital",
    "ownersPortalSettings",
    "accountId",
)
# Card, bank and payment method details of the payments fixture.
PAYMENT_NEVER_SHOWN = (
    "pm_fake_555",
    "pi_fake_confirm_666",
    "po_fake_777",
    "9917",
    "VisaFake",
    "411199",
    "FAKE-IBAN-GB00-0000",
    "Secret refund note",
    "Declined for FAKE reason",
    "card_declined",
    "424242",
    "acct-fake-pay-1",
)

__all__ = [
    "BASE",
    "OWNERS_URL",
    "OWNER_EMAIL",
    "OWNER_ID",
    "OWNER_NAME",
    "OWNER_NEVER_REQUESTED",
    "OWNER_NEVER_SHOWN",
    "OWNER_PHONE",
    "PAYMENTS_URL",
    "PAYMENT_NEVER_SHOWN",
    "RESERVATION_ID",
    "StandInTokens",
    "fixture",
    "json_response",
    "make_client",
    "mock_router",
]


def fixture(area: str, name: str) -> object:
    return json.loads((FIXTURES / area / name).read_text())
