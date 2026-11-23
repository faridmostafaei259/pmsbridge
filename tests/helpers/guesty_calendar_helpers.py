"""Helpers for the Guesty calendar and pricing tests: invented fixtures and fake day lists.

Nothing here talks to the real service. The client and router come from `guesty_client_helpers`.
"""

import json
from datetime import date, timedelta
from pathlib import Path

from guesty_client_helpers import BASE, json_response, make_client, mock_router

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty" / "calendar"
CALENDAR_URL = f"{BASE}/availability-pricing/api/calendar/listings"
LISTING_ID = "6a3000000000000000000001"
PUT_URL = f"{CALENDAR_URL}/{LISTING_ID}"
LISTING_URL = f"{BASE}/listings/{LISTING_ID}"
# An invented staff note that must never appear in a result.
STAFF_NOTE = "STAFF-NOTE-INVENTED"

__all__ = [
    "BASE",
    "CALENDAR_URL",
    "LISTING_ID",
    "LISTING_URL",
    "PUT_URL",
    "STAFF_NOTE",
    "fake_days",
    "fixture",
    "json_response",
    "make_client",
    "mock_router",
    "put_bodies",
]


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def fake_days(
    start: str,
    end: str,
    *,
    price: float = 150,
    currency: str = "EUR",
    booked: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    """One available day per date from start to end (inclusive); `booked` dates have a booking."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days: list[dict[str, object]] = []
    for offset in range((last - first).days + 1):
        text = (first + timedelta(days=offset)).isoformat()
        day: dict[str, object] = {
            "date": text,
            "listingId": LISTING_ID,
            "currency": currency,
            "price": price,
            "minNights": 2,
            "status": "available",
        }
        if text in booked:
            day |= {"status": "booked", "reservationId": "6a3000000000000000000b01"}
        days.append(day)
    return days


def put_bodies(route: object) -> list[object]:
    """The JSON bodies sent to a respx route, in order."""
    return [json.loads(call.request.content) for call in route.calls]  # type: ignore[attr-defined]
