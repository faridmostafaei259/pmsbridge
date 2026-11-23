"""Reading the Guesty calendar: the day mapper, both response shapes and the read request."""

import logging
import time
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import respx
from guesty_calendar_helpers import (
    CALENDAR_URL,
    LISTING_ID,
    STAFF_NOTE,
    fake_days,
    fixture,
    json_response,
    make_client,
    mock_router,
)

from pmsbridge.pms.base import (
    Capability,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
    call_deadline,
)
from pmsbridge.pms.guesty import calendar
from pmsbridge.pms.guesty.provider import DOMAIN_MODULES, discover_domains
from pmsbridge.pms.models import CalendarDay, CalendarRange

JUNE = CalendarRange(listing_id=LISTING_ID, start=date(2030, 6, 1), end=date(2030, 6, 8))


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        yield mocked


def by_day(days: list[CalendarDay]) -> dict[str, CalendarDay]:
    return {day.day.isoformat(): day for day in days}


def test_the_module_is_wired_into_the_provider() -> None:
    assert {Capability.CALENDAR_READ, Capability.CALENDAR_WRITE} == calendar.CAPABILITIES
    assert DOMAIN_MODULES["calendar"] == "calendar"
    assert "calendar" in discover_domains()


def test_a_bare_array_is_mapped_day_by_day() -> None:
    days = calendar.map_calendar(
        fixture("days_array.json"), LISTING_ID, date(2030, 6, 1), date(2030, 6, 8)
    )
    shown = by_day(days)
    # Outside the range, another listing, and no date: dropped. Nothing is invented for 06-08.
    assert list(shown) == [f"2030-06-0{n}" for n in range(1, 8)]
    open_day = shown["2030-06-01"]
    assert open_day.available is True and open_day.min_nights == 2
    assert open_day.price is not None
    assert (open_day.price.amount, open_day.price.currency) == (Decimal("150"), "EUR")
    assert open_day.reservation_id is None and open_day.blocked_reason is None
    assert open_day.extras == {"pms_status": "available"}


def test_a_booked_day_has_its_reservation_and_no_block_reason() -> None:
    days = calendar.map_calendar(
        fixture("days_array.json"), LISTING_ID, date(2030, 6, 1), date(2030, 6, 8)
    )
    booked = by_day(days)["2030-06-02"]
    assert booked.available is False
    assert booked.reservation_id == "6a3000000000000000000b01"
    assert booked.blocked_reason is None


def test_a_blocked_day_says_blocked_and_never_copies_staff_notes() -> None:
    days = calendar.map_calendar(
        fixture("days_array.json"), LISTING_ID, date(2030, 6, 1), date(2030, 6, 9)
    )
    shown = by_day(days)
    assert shown["2030-06-03"].available is False
    assert shown["2030-06-03"].blocked_reason == "blocked"
    assert shown["2030-06-09"].blocked_reason == "blocked"  # an unknown block key
    assert STAFF_NOTE not in repr(days) and "gate code" not in repr(days)


def test_the_unit_count_decides_when_there_is_one() -> None:
    days = calendar.map_calendar(
        fixture("days_array.json"), LISTING_ID, date(2030, 6, 1), date(2030, 6, 8)
    )
    shown = by_day(days)
    assert shown["2030-06-04"].available is False  # status available but no unit left
    assert shown["2030-06-05"].available is True  # status unavailable but two units left
    assert shown["2030-06-05"].extras == {"pms_status": "unavailable", "allotment": 2}


def test_a_day_without_status_or_count_is_never_shown_as_free() -> None:
    days = calendar.map_calendar(
        fixture("days_array.json"), LISTING_ID, date(2030, 6, 1), date(2030, 6, 8)
    )
    unknown = by_day(days)["2030-06-06"]
    assert unknown.available is False and unknown.blocked_reason is None
    assert unknown.price is not None and unknown.price.amount == Decimal("99.5")


def test_odd_values_are_left_out_not_guessed() -> None:
    days = calendar.map_calendar(
        fixture("days_array.json"), LISTING_ID, date(2030, 6, 1), date(2030, 6, 8)
    )
    odd = by_day(days)["2030-06-07"]
    assert odd.available is True  # the status is read in any case
    assert odd.price is None and odd.min_nights is None


def test_the_envelope_shape_uses_its_currency_and_keeps_the_first_day_for_a_date() -> None:
    days = calendar.map_calendar(
        fixture("days_envelope.json"), LISTING_ID, date(2030, 6, 1), date(2030, 6, 3)
    )
    assert [day.day.isoformat() for day in days] == ["2030-06-01", "2030-06-02", "2030-06-03"]
    first = days[0]
    assert first.price is not None
    assert (first.price.amount, first.price.currency) == (Decimal("210.5"), "USD")
    assert days[1].available is False and days[1].blocked_reason == "blocked"


@pytest.mark.parametrize(
    "body",
    [
        {"days": [{"date": "2030-06-01", "status": "available", "price": 100, "currency": "EUR"}]},
        {"data": [{"date": "2030-06-01", "status": "available", "price": 100, "currency": "EUR"}]},
        {
            "results": [
                {"date": "2030-06-01", "status": "available", "price": 100, "currency": "EUR"}
            ]
        },
    ],
)
def test_other_envelope_keys_are_accepted(body: object) -> None:
    days = calendar.map_calendar(body, LISTING_ID, date(2030, 6, 1), date(2030, 6, 1))
    assert len(days) == 1


@pytest.mark.parametrize(
    "body", ["ok", 5, None, {"message": "nope"}, {"data": {"data": {"data": {"x": 1}}}}]
)
def test_an_answer_that_is_not_a_calendar_is_an_upstream_error(body: object) -> None:
    with pytest.raises(PMSUpstreamError, match="could not be understood"):
        calendar.map_calendar(body, LISTING_ID, date(2030, 6, 1), date(2030, 6, 2))


def test_an_empty_calendar_is_empty_not_an_error() -> None:
    assert calendar.map_calendar([], LISTING_ID, date(2030, 6, 1), date(2030, 6, 2)) == []


def test_skipped_days_are_logged_without_their_content(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        calendar.map_calendar(
            [{"price": 777, "status": "available"}], LISTING_ID, date(2030, 6, 1), date(2030, 6, 1)
        )
    assert "777" not in caplog.text


async def test_the_read_sends_one_listing_and_the_dates(router: respx.MockRouter) -> None:
    route = router.get(CALENDAR_URL).mock(return_value=json_response(fixture("days_array.json")))
    days = await calendar.build(make_client()).get_calendar(JUNE)
    assert len(days) == 7
    assert dict(route.calls.last.request.url.params) == {
        "listingIds": LISTING_ID,
        "startDate": "2030-06-01",
        "endDate": "2030-06-08",
    }
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


async def test_a_listing_id_with_a_comma_is_refused_before_any_request(
    router: respx.MockRouter,
) -> None:
    route = router.get(CALENDAR_URL).mock(return_value=json_response([]))
    wanted = CalendarRange(listing_id="a,b", start=JUNE.start, end=JUNE.end)
    with pytest.raises(PMSValidationError):
        await calendar.build(make_client()).get_calendar(wanted)
    assert route.call_count == 0


async def test_a_missing_listing_is_a_plain_not_found(router: respx.MockRouter) -> None:
    router.get(CALENDAR_URL).mock(
        return_value=json_response({"message": "private text", "code": "LISTING_NOT_FOUND"}, 404)
    )
    with pytest.raises(PMSNotFoundError) as caught:
        await calendar.build(make_client()).get_calendar(JUNE)
    assert "private text" not in caught.value.message


async def test_a_full_year_is_mapped_within_bounds(router: respx.MockRouter) -> None:
    router.get(CALENDAR_URL).mock(return_value=json_response(fake_days("2030-01-01", "2030-12-31")))
    wide = CalendarRange(listing_id=LISTING_ID, start=date(2030, 1, 1), end=date(2030, 12, 31))
    assert len(await calendar.build(make_client()).get_calendar(wide)) == 365


async def test_an_expired_deadline_sends_nothing(router: respx.MockRouter) -> None:
    route = router.get(CALENDAR_URL).mock(return_value=json_response([]))
    token = call_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(PMSUpstreamError, match="ran out"):
            await calendar.build(make_client()).get_calendar(JUNE)
    finally:
        call_deadline.reset(token)
    assert route.call_count == 0


@pytest.mark.parametrize("booked_first", [True, False])
def test_a_date_with_one_booked_entry_counts_as_booked(booked_first: bool) -> None:
    free = {"date": "2030-06-01", "status": "available", "price": 100, "currency": "EUR"}
    booked = {"date": "2030-06-01", "status": "booked", "reservationId": "r1", "currency": "EUR"}
    entries = [booked, free] if booked_first else [free, booked]
    (day,) = calendar.map_calendar(entries, LISTING_ID, date(2030, 6, 1), date(2030, 6, 1))
    assert day.reservation_id == "r1" and day.extras["booked"] is True


def test_a_reservation_object_counts_as_booked() -> None:
    entry = {
        "date": "2030-06-01",
        "status": "unavailable",
        "price": 100,
        "currency": "EUR",
        "reservation": {"guestName": "Invented Person"},
    }
    (day,) = calendar.map_calendar([entry], LISTING_ID, date(2030, 6, 1), date(2030, 6, 1))
    assert day.extras["booked"] is True and day.blocked_reason is None
    assert "Invented Person" not in repr(day)
