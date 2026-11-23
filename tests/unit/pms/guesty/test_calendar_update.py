"""Changing the Guesty calendar: the refusals, one PUT per range, partial failures, no resends."""

import logging
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from guesty_calendar_helpers import (
    CALENDAR_URL,
    LISTING_ID,
    LISTING_URL,
    PUT_URL,
    fake_days,
    json_response,
    make_client,
    mock_router,
    put_bodies,
)

from pmsbridge.pms.base import (
    PMSAuthError,
    PMSNotFoundError,
    PMSRateLimitedError,
    PMSUpstreamError,
    PMSValidationError,
)
from pmsbridge.pms.guesty import calendar
from pmsbridge.pms.models import CalendarUpdate, CalendarUpdateRequest, Money

READ_FAILURE = (
    "The changes were applied but the updated calendar could not be read; check it in Guesty."
)


@pytest.fixture
async def router() -> AsyncIterator[respx.MockRouter]:
    with mock_router() as mocked:
        listing(mocked)  # the listing record: EUR, base price 150 (tests may change it)
        yield mocked


def euro(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="EUR")


def update(start: str, end: str, **fields: object) -> CalendarUpdate:
    return CalendarUpdate(start=date.fromisoformat(start), end=date.fromisoformat(end), **fields)  # type: ignore[arg-type]


def request(*updates: CalendarUpdate) -> CalendarUpdateRequest:
    return CalendarUpdateRequest(listing_id=LISTING_ID, updates=list(updates))


def three() -> CalendarUpdateRequest:
    return request(
        update("2030-12-20", "2030-12-23", price=euro("180"), min_nights=3),
        update("2030-12-24", "2030-12-25", available=False),
        update("2030-12-26", "2030-12-26", available=True),
    )


def calendar_reads(router: respx.MockRouter, *bodies: object | httpx.Response) -> respx.Route:
    """The calendar read answers with each body in turn (a before-read, then an after-read)."""
    answers = [b if isinstance(b, httpx.Response) else json_response(b) for b in bodies]
    return router.get(CALENDAR_URL).mock(side_effect=answers)


def put_route(router: respx.MockRouter, *answers: httpx.Response | Exception) -> respx.Route:
    return router.put(PUT_URL).mock(side_effect=list(answers) or [json_response("ok")] * 31)


def listing(
    router: respx.MockRouter, *, currency: str | None = "EUR", base: float | None = 150
) -> respx.Route:
    """The listing record that holds the currency and base price (`prices`)."""
    prices: dict[str, object] = {"basePriceUSD": 99}
    if currency is not None:
        prices["currency"] = currency
    if base is not None:
        prices["basePrice"] = base
    return router.get(LISTING_URL).mock(
        return_value=json_response({"_id": LISTING_ID, "prices": prices})
    )


def methods(router: respx.MockRouter) -> list[str]:
    return [call.request.method for call in router.calls]


# ---- Success ----


async def test_each_range_is_one_put_with_only_the_fields_that_were_set(
    router: respx.MockRouter,
) -> None:
    window = fake_days("2030-12-19", "2030-12-27")
    reads = calendar_reads(router, window, window)
    puts = put_route(router)
    days = await calendar.build(make_client()).update_calendar(three())
    assert put_bodies(puts) == [
        {"startDate": "2030-12-20", "endDate": "2030-12-23", "price": 180.0, "minNights": 3},
        {
            "startDate": "2030-12-24",
            "endDate": "2030-12-25",
            "status": "unavailable",
            "blockReason": "Other",
        },
        {"startDate": "2030-12-26", "endDate": "2030-12-26", "status": "available"},
    ]
    assert methods(router) == ["GET", "GET", "PUT", "PUT", "PUT", "GET"]  # listing, calendar
    # Both reads cover the union of the ranges, and the result is only the changed days.
    for call in reads.calls:
        assert dict(call.request.url.params) == {
            "listingIds": LISTING_ID,
            "startDate": "2030-12-20",
            "endDate": "2030-12-26",
        }
    assert [day.day.isoformat() for day in days] == [f"2030-12-{n}" for n in range(20, 27)]
    assert puts.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


async def test_the_result_is_the_calendar_as_it_is_afterwards(router: respx.MockRouter) -> None:
    calendar_reads(
        router,
        fake_days("2030-12-20", "2030-12-21", price=150),
        fake_days("2030-12-20", "2030-12-21", price=180),
    )
    put_route(router)
    days = await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-21", price=euro("180")))
    )
    assert [day.price.amount for day in days if day.price] == [Decimal("180")] * 2


async def test_a_price_on_a_booked_day_is_allowed(router: respx.MockRouter) -> None:
    window = fake_days("2030-12-20", "2030-12-22", booked=("2030-12-21",))
    calendar_reads(router, window, window)
    puts = put_route(router)
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-22", price=euro("160"), min_nights=2))
    )
    assert puts.call_count == 1


@pytest.mark.parametrize("amount", ["37.50", "600"])
async def test_a_price_at_the_edge_of_the_range_is_accepted(
    router: respx.MockRouter, amount: str
) -> None:
    window = fake_days("2030-12-20", "2030-12-21", price=150)
    calendar_reads(router, window, window)
    puts = put_route(router)
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-21", price=euro(amount)))
    )
    assert puts.call_count == 1


# ---- Refusals: nothing is sent ----


async def refused(router: respx.MockRouter, body: object, *updates: CalendarUpdate) -> str:
    calendar_reads(router, body)
    puts = put_route(router)
    with pytest.raises(PMSValidationError) as caught:
        await calendar.build(make_client()).update_calendar(request(*updates))
    assert puts.call_count == 0  # no PUT was sent
    assert "PUT" not in methods(router)
    assert "Nothing was changed" in caught.value.message
    return caught.value.message


async def test_a_price_in_another_currency_is_refused(router: respx.MockRouter) -> None:
    message = await refused(
        router,
        fake_days("2030-12-20", "2030-12-21"),
        update("2030-12-20", "2030-12-21", price=Money(amount=Decimal("150"), currency="USD")),
    )
    assert message == (
        "Range 1: the price for 2030-12-20 to 2030-12-21 is in USD, but this listing uses EUR. "
        "Nothing was changed."
    )


async def test_the_listing_record_decides_the_currency(router: respx.MockRouter) -> None:
    listing(router, currency="USD")
    # The calendar shows euro prices, but Guesty says the listing uses dollars.
    message = await refused(
        router,
        fake_days("2030-12-20", "2030-12-21", currency="EUR"),
        update("2030-12-20", "2030-12-21", price=Money(amount=Decimal("150"), currency="USD")),
    )
    assert "shows no price in USD" in message


@pytest.mark.parametrize("amount", ["37.49", "5", "600.01", "9000"])
async def test_a_price_far_from_the_base_price_is_refused(
    router: respx.MockRouter, amount: str
) -> None:
    message = await refused(
        router,
        fake_days("2030-12-20", "2030-12-21", price=150),
        update("2030-12-20", "2030-12-21", price=euro(amount)),
    )
    assert message.startswith("Range 1: this price looks like a mistake")
    assert "base price of 150 EUR" in message
    assert message.endswith("If the price is intended, change it in Guesty directly.")


@pytest.mark.parametrize("amount", ["37.50", "600"])
async def test_a_price_at_the_edge_of_the_allowed_range_is_accepted(
    router: respx.MockRouter, amount: str
) -> None:
    window = fake_days("2030-12-20", "2030-12-21", price=150)
    calendar_reads(router, window, window)
    puts = put_route(router)
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-21", price=euro(amount)))
    )
    assert puts.call_count == 1


async def test_each_day_is_checked_against_its_own_price_in_a_mixed_season(
    router: respx.MockRouter,
) -> None:
    days = fake_days("2030-12-20", "2030-12-20", price=100) + fake_days(
        "2030-12-21", "2030-12-21", price=400
    )
    # 99 would take the 400 day below a quarter of its price; 401 would take the 100 day above
    # four times its price. Both are inside the listing's base-price band (37.50 to 600).
    for amount in ("99", "401"):
        message = await refused(
            router, days, update("2030-12-20", "2030-12-21", price=euro(amount))
        )
        assert "run from 100 to 400 EUR" in message
        router.reset()
    calendar_reads(router, days, days, days, days)
    puts = put_route(router)
    for amount in ("100", "400"):
        await calendar.build(make_client()).update_calendar(
            request(update("2030-12-20", "2030-12-21", price=euro(amount)))
        )
    assert puts.call_count == 2


async def test_a_second_step_of_a_ratchet_is_refused(router: respx.MockRouter) -> None:
    window = fake_days("2030-12-20", "2030-12-21", price=150)
    raised = fake_days("2030-12-20", "2030-12-21", price=500)
    calendar_reads(router, window, raised)
    puts = put_route(router)
    api = calendar.build(make_client())
    await api.update_calendar(request(update("2030-12-20", "2030-12-21", price=euro("500"))))
    assert puts.call_count == 1
    # The calendar now shows 500, so 1500 is inside the per-day limit (4 x 500), but it is far
    # above the listing's own base price of 150.
    calendar_reads(router, raised)
    with pytest.raises(PMSValidationError, match="base price of 150 EUR"):
        await api.update_calendar(request(update("2030-12-20", "2030-12-21", price=euro("1500"))))
    assert puts.call_count == 1


async def test_a_listing_without_a_base_price_or_currency_refuses_prices(
    router: respx.MockRouter,
) -> None:
    days = fake_days("2030-12-20", "2030-12-21")
    listing(router, base=None)
    message = await refused(router, days, update("2030-12-20", "2030-12-21", price=euro("150")))
    assert "no base price" in message
    listing(router, currency=None)
    message = await refused(router, days, update("2030-12-20", "2030-12-21", price=euro("150")))
    assert "did not say which currency" in message


async def test_the_listing_is_read_only_for_changes_that_set_a_price(
    router: respx.MockRouter,
) -> None:
    listing_route = listing(router, currency=None, base=None)
    window = fake_days("2030-12-20", "2030-12-21")
    calendar_reads(router, window, window)
    puts = put_route(router)
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-21", min_nights=3, available=False))
    )
    assert puts.call_count == 1 and listing_route.call_count == 0


async def test_a_price_update_reads_the_listing_with_only_its_prices(
    router: respx.MockRouter,
) -> None:
    listing_route = listing(router)
    window = fake_days("2030-12-20", "2030-12-21")
    calendar_reads(router, window, window)
    put_route(router)
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-21", price=euro("150")))
    )
    assert listing_route.call_count == 1
    assert dict(listing_route.calls.last.request.url.params) == {"fields": "_id prices"}


@pytest.mark.parametrize("available", [True, False])
async def test_an_availability_change_on_a_booked_day_is_refused(
    router: respx.MockRouter, available: bool
) -> None:
    message = await refused(
        router,
        fake_days("2030-12-20", "2030-12-23", booked=("2030-12-22",)),
        update("2030-12-20", "2030-12-23", available=available),
    )
    assert message.startswith(
        "Range 1: availability cannot be changed for 2030-12-20 to 2030-12-23"
    )
    assert "has a reservation" in message
    assert "6a3000000000000000000b01" not in message


async def test_a_date_that_is_booked_in_any_entry_is_protected(router: respx.MockRouter) -> None:
    days = fake_days("2030-12-20", "2030-12-22")
    duplicate = fake_days("2030-12-21", "2030-12-21", booked=("2030-12-21",))
    message = await refused(
        router, days + duplicate, update("2030-12-20", "2030-12-22", available=False)
    )
    assert "has a reservation" in message


async def test_a_day_with_a_reservation_object_is_protected(router: respx.MockRouter) -> None:
    days = fake_days("2030-12-20", "2030-12-21")
    days[1] |= {"status": "unavailable", "reservation": {"guestName": "Invented Person"}}
    message = await refused(router, days, update("2030-12-20", "2030-12-21", available=True))
    assert "has a reservation" in message and "Invented Person" not in message


async def test_availability_cannot_change_on_multi_unit_days(router: respx.MockRouter) -> None:
    days = fake_days("2030-12-20", "2030-12-21")
    days[0]["allotment"] = 2
    message = await refused(router, days, update("2030-12-20", "2030-12-21", available=False))
    assert "several units" in message
    # A price or a minimum stay on those days is allowed.
    calendar_reads(router, days, days)
    puts = put_route(router)
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-21", min_nights=2))
    )
    assert puts.call_count == 1


async def test_a_booked_day_outside_the_range_does_not_matter(router: respx.MockRouter) -> None:
    window = fake_days("2030-12-20", "2030-12-25", booked=("2030-12-25",))
    calendar_reads(router, window, window)
    puts = put_route(router)
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-23", available=False))
    )
    assert puts.call_count == 1


async def test_a_shortened_calendar_answer_refuses_every_kind_of_change(
    router: respx.MockRouter,
) -> None:
    short = fake_days("2030-12-20", "2030-12-21")  # the range runs to the 23rd
    for change in (
        update("2030-12-20", "2030-12-23", price=euro("150")),
        update("2030-12-20", "2030-12-23", available=False),
        update("2030-12-20", "2030-12-23", min_nights=2),
    ):
        message = await refused(router, short, change)
        assert message.startswith("Range 1 (2030-12-20 to 2030-12-23) was not sent")
        assert "does not show every day" in message
        router.reset()


async def test_days_without_a_price_in_the_listing_currency_refuse_a_price(
    router: respx.MockRouter,
) -> None:
    days = fake_days("2030-12-20", "2030-12-22")
    del days[1]["price"]
    message = await refused(router, days, update("2030-12-20", "2030-12-22", price=euro("150")))
    assert "shows no price in EUR for some of these days" in message
    router.reset()
    calendar_reads(router, days, days)
    puts = put_route(router)  # but a minimum stay does not need a price
    await calendar.build(make_client()).update_calendar(
        request(update("2030-12-20", "2030-12-22", min_nights=2))
    )
    assert puts.call_count == 1


async def test_one_refused_range_stops_the_whole_request(router: respx.MockRouter) -> None:
    message = await refused(
        router,
        fake_days("2030-12-20", "2030-12-26", booked=("2030-12-26",)),
        update("2030-12-20", "2030-12-21", price=euro("150")),
        update("2030-12-26", "2030-12-26", available=False),
    )
    assert message.startswith("Range 2:") and "2030-12-26 to 2030-12-26" in message


async def test_a_range_that_starts_before_yesterday_is_refused_with_no_request(
    router: respx.MockRouter,
) -> None:
    today = date.today()
    reads = calendar_reads(router, [])
    puts = put_route(router)
    older = CalendarUpdate(
        start=today - timedelta(days=2), end=today + timedelta(days=3), min_nights=2
    )
    with pytest.raises(PMSValidationError, match=r"Range 1 starts on .*, before yesterday"):
        await calendar.build(make_client()).update_calendar(request(older))
    assert reads.call_count == 0 and puts.call_count == 0 and methods(router) == []
    # Yesterday itself is allowed (listing time zones can be ahead of the server).
    window = fake_days(
        (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()
    )
    calendar_reads(router, window, window)
    await calendar.build(make_client()).update_calendar(
        request(CalendarUpdate(start=today - timedelta(days=1), end=today, min_nights=2))
    )
    assert puts.call_count == 1


async def test_ranges_that_span_more_than_a_year_are_refused_with_no_request(
    router: respx.MockRouter,
) -> None:
    reads = calendar_reads(router, [])
    puts = put_route(router)
    wide = request(
        update("2030-01-10", "2030-01-11", min_nights=2),
        update("2031-01-10", "2031-01-11", min_nights=2),
    )
    with pytest.raises(PMSValidationError, match="Send them in separate requests"):
        await calendar.build(make_client()).update_calendar(wide)
    assert reads.call_count == 0 and puts.call_count == 0


async def test_a_failed_first_read_sends_nothing(router: respx.MockRouter) -> None:
    calendar_reads(router, json_response({"message": "private"}, 404))
    puts = put_route(router)
    with pytest.raises(PMSNotFoundError):
        await calendar.build(make_client()).update_calendar(
            request(update("2030-12-20", "2030-12-21", min_nights=2))
        )
    assert puts.call_count == 0


async def test_a_listing_id_with_a_comma_is_refused(router: respx.MockRouter) -> None:
    reads = calendar_reads(router, [])
    bad = request(update("2030-12-20", "2030-12-21", min_nights=2)).model_copy(
        update={"listing_id": "a,b"}
    )
    with pytest.raises(PMSValidationError):
        await calendar.build(make_client()).update_calendar(bad)
    assert reads.call_count == 0


# ---- Failures part-way ----


async def test_a_server_error_on_the_second_range_is_reported_exactly(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    reads = calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    puts = put_route(router, json_response("ok"), json_response({"message": "private"}, 500))
    with caplog.at_level(logging.INFO), pytest.raises(PMSUpstreamError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert caught.value.message == (
        "Range 2 of 3 (2030-12-24 to 2030-12-25) did not complete and may or may not have been "
        "applied. Range 1 was applied. Range 3 was not sent. Check the calendar before trying "
        "again: the outcome of range 2 is unknown."
    )
    assert puts.call_count == 2  # the failed one was not repeated and the third was not sent
    assert reads.call_count == 1  # and the calendar was not read again
    assert "private" not in caught.value.message and "private" not in caplog.text
    assert "180" not in caplog.text


async def test_a_503_on_a_range_is_never_sent_again(router: respx.MockRouter) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    puts = put_route(router, json_response({}, 503))
    with pytest.raises(PMSUpstreamError, match="may or may not have been applied"):
        await calendar.build(make_client()).update_calendar(three())
    assert puts.call_count == 1


async def test_a_timeout_on_the_second_range_means_the_outcome_is_unknown(
    router: respx.MockRouter,
) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    puts = put_route(router, json_response("ok"), httpx.ReadTimeout("slow"))
    with pytest.raises(PMSUpstreamError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    message = caught.value.message
    assert message.startswith("Range 2 of 3 (2030-12-24 to 2030-12-25) did not complete")
    assert "Range 1 was applied. Range 3 was not sent." in message
    assert "the outcome of range 2 is unknown" in message
    assert puts.call_count == 2


async def test_a_dropped_connection_on_the_last_range_is_unknown_and_has_nothing_after_it(
    router: respx.MockRouter,
) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    put_route(router, json_response("ok"), json_response("ok"), httpx.ReadError("dropped"))
    with pytest.raises(PMSUpstreamError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert caught.value.message == (
        "Range 3 of 3 (2030-12-26 to 2030-12-26) did not complete and may or may not have been "
        "applied. Ranges 1 to 2 were applied. Check the calendar before trying again: the "
        "outcome of range 3 is unknown."
    )


async def test_a_rejected_first_range_says_nothing_was_changed(router: respx.MockRouter) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    puts = put_route(router, json_response({"message": "private"}, 422))
    with pytest.raises(PMSValidationError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert caught.value.message == (
        "Range 1 of 3 (2030-12-20 to 2030-12-23) was not applied: Guesty rejected it as "
        "invalid. Nothing was changed. Ranges 2 to 3 were not sent."
    )
    assert puts.call_count == 1


async def test_a_rejection_after_applied_ranges_does_not_say_nothing_was_changed(
    router: respx.MockRouter,
) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    put_route(router, json_response("ok"), json_response("ok"), json_response({}, 400))
    with pytest.raises(PMSValidationError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert "Nothing was changed" not in caught.value.message
    assert "Ranges 1 to 2 were applied." in caught.value.message


async def test_a_429_without_a_wait_time_is_not_sent_again(router: respx.MockRouter) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    puts = put_route(router, json_response("ok"), json_response({}, 429))
    with pytest.raises(PMSRateLimitedError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert puts.call_count == 2
    assert "Range 2 of 3" in caught.value.message and "Range 1 was applied." in caught.value.message
    assert caught.value.retry_after_seconds is None


async def test_a_429_with_a_wait_time_is_sent_again_once_it_has_waited(
    router: respx.MockRouter,
) -> None:
    window = fake_days("2030-12-19", "2030-12-27")
    calendar_reads(router, window, window)
    puts = put_route(
        router,
        httpx.Response(429, headers={"Retry-After": "0"}),
        json_response("ok"),
        json_response("ok"),
        json_response("ok"),
    )
    await calendar.build(make_client()).update_calendar(three())
    assert puts.call_count == 4  # the first range twice (the 429 was not processed), then two more
    assert put_bodies(puts)[0] == put_bodies(puts)[1]


async def test_a_failed_read_after_the_changes_says_they_were_applied(
    router: respx.MockRouter,
) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"), json_response({}, 404))
    puts = put_route(router)
    with pytest.raises(PMSUpstreamError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert caught.value.message == READ_FAILURE
    assert puts.call_count == 3


async def test_a_deadline_that_runs_out_between_ranges_stops_before_the_next_one(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    puts = put_route(router)
    left = iter([30.0, 1.5])  # plenty of time for range 1, too little for range 2
    monkeypatch.setattr(calendar, "remaining_seconds", lambda: next(left))
    with pytest.raises(PMSUpstreamError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert caught.value.message == (
        "Range 2 and later were not sent: the time allowed ran out. Range 1 was applied. "
        "Check the calendar before trying again."
    )
    assert puts.call_count == 1 and reads.call_count == 1  # and no re-read


async def test_a_deadline_that_is_already_short_sends_nothing(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calendar_reads(router, fake_days("2030-12-19", "2030-12-27"))
    puts = put_route(router)
    monkeypatch.setattr(calendar, "remaining_seconds", lambda: 0.5)
    with pytest.raises(PMSUpstreamError) as caught:
        await calendar.build(make_client()).update_calendar(three())
    assert caught.value.message == (
        "Range 1 and later were not sent: the time allowed ran out. Nothing was changed."
    )
    assert puts.call_count == 0


def test_a_token_that_was_not_accepted_is_worded_neutrally() -> None:
    error = calendar.partial_failure(
        PMSAuthError("private"),
        2,
        3,
        update("2030-12-24", "2030-12-25", available=False),
    )
    assert isinstance(error, PMSAuthError)
    assert error.message == (
        "Range 2 of 3 (2030-12-24 to 2030-12-25) was not applied: Guesty did not accept the "
        "access token. Range 1 was applied. Range 3 was not sent."
    )
