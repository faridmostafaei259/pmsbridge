"""The date and money readers added to the shared Guesty helpers, and domain independence."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pmsbridge.pms.guesty import common, reservations


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2027-03-10", date(2027, 3, 10)),
        (" 2027-03-10 ", date(2027, 3, 10)),
        ("2027-02-30", None),
        ("2027-3-1", None),
        ("2027-03-10T00:00:00Z", None),
        ("", None),
        (20270310, None),
        (None, None),
    ],
)
def test_date_of(raw: object, expected: date | None) -> None:
    assert common.date_of(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2027-01-05T09:30:00.000Z", datetime(2027, 1, 5, 9, 30, tzinfo=UTC)),
        ("2027-01-05T09:30:00+02:00", datetime(2027, 1, 5, 7, 30, tzinfo=UTC)),
        ("2027-01-05T09:30:00", None),
        ("2027-01-05", None),
        ("yesterday", None),
        (5, None),
        (None, None),
    ],
)
def test_datetime_of_needs_an_offset_by_default(raw: object, expected: datetime | None) -> None:
    found = common.datetime_of(raw)
    assert found == expected
    if found is not None:
        assert found.tzinfo is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2027-01-05T09:30:00", datetime(2027, 1, 5, 9, 30, tzinfo=UTC)),
        ("2027-01-05T09:30:00+02:00", datetime(2027, 1, 5, 7, 30, tzinfo=UTC)),
        ("2027-01-05", None),  # a bare date is never an instant
        ("yesterday", None),
    ],
)
def test_datetime_of_can_read_a_documented_utc_value_without_an_offset(
    raw: object, expected: datetime | None
) -> None:
    assert common.datetime_of(raw, naive_as_utc=True) == expected


def test_money_of() -> None:
    money = common.money_of(125.5, "EUR")
    assert money is not None and money.amount == Decimal("125.5") and money.currency == "EUR"
    negative = common.money_of(-3, "USD")
    assert negative is not None and negative.amount == Decimal(-3)
    lower = common.money_of(2, " eur ")
    assert lower is not None and lower.currency == "EUR"
    for amount, currency in (
        (True, "EUR"),
        ("5", "EUR"),
        (float("inf"), "EUR"),
        (5, "EURO"),
        (5, None),
        (None, "EUR"),
    ):
        assert common.money_of(amount, currency) is None


def test_the_reservations_module_does_not_import_another_domain() -> None:
    source = Path(reservations.__file__ or "").read_text()
    for other in ("listings", "content", "guests", "calendar", "inbox"):
        assert f"pms.guesty.{other}" not in source
        assert f"pms.guesty import {other}" not in source
