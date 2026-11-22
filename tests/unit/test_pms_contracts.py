import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pmsbridge.pms.base import (
    WRITE_CAPABILITIES,
    Capability,
    CapabilityNotSupportedError,
    PMSProvider,
    PMSValidationError,
    Provider,
    call_deadline,
    remaining_seconds,
    require_api,
)
from pmsbridge.pms.models import (
    MAX_CALENDAR_UPDATES,
    MAX_EXTRAS_KEYS,
    MAX_PAGE_SIZE,
    MAX_TEXT_LENGTH,
    TRUNCATION_MARKER,
    CalendarDay,
    CalendarRange,
    CalendarUpdate,
    CalendarUpdateRequest,
    Listing,
    ListingQuery,
    Message,
    MessageChannel,
    MessageDirection,
    Money,
    OutgoingMessage,
    Page,
    PageRequest,
    Reservation,
    ReservationNote,
    ReservationQuery,
    ReviewReply,
    TaskQuery,
)
from pmsbridge.pms.testing import FakeProvider, as_provider

TOMORROW = date.today() + timedelta(days=1)


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


# ----------------------------------------------------------------- shared values


def test_money_requires_iso_currency() -> None:
    assert usd("120.50").currency == "USD"
    with pytest.raises(ValidationError):
        Money(amount=Decimal(1), currency="usd")


def test_page_request_limits() -> None:
    assert PageRequest().limit == 25
    with pytest.raises(ValidationError):
        PageRequest(limit=MAX_PAGE_SIZE + 1)
    with pytest.raises(ValidationError):
        PageRequest(limit=0)


def test_models_are_frozen_and_strict() -> None:
    listing = Listing(id="l1", title="Sea view")
    with pytest.raises(ValidationError):
        listing.title = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Listing(id="l1", title="x", unknown_field=1)  # type: ignore[call-arg]


def test_generic_page() -> None:
    page = Page[Listing](items=[Listing(id="l1", title="A")], next_cursor="c2")
    assert page.items[0].id == "l1"


# ----------------------------------------------------------------- read models truncate


def test_long_read_text_is_truncated_not_rejected() -> None:
    msg = Message(
        id="m1", conversation_id="c1", direction=MessageDirection.INBOUND, body="x" * 6000
    )
    assert len(msg.body) == MAX_TEXT_LENGTH
    assert msg.body.endswith(TRUNCATION_MARKER)
    assert msg.sent_at is None


def test_extras_are_capped_and_scalar() -> None:
    Listing(id="l1", title="A", extras={"floor": 3, "view": "sea", "pets": True})
    with pytest.raises(ValidationError):
        Listing(id="l1", title="A", extras={f"k{i}": i for i in range(MAX_EXTRAS_KEYS + 1)})
    with pytest.raises(ValidationError):
        Listing(id="l1", title="A", extras={"raw": {"nested": "payload"}})  # type: ignore[dict-item]


# ----------------------------------------------------------------- reservations


def test_reservation_dates_and_nights_validated() -> None:
    with pytest.raises(ValidationError):
        Reservation(
            id="r1",
            listing_id="l1",
            check_in=date(2026, 5, 3),
            check_out=date(2026, 5, 1),
            nights=0,
        )
    with pytest.raises(ValidationError):
        Reservation(
            id="r1",
            listing_id="l1",
            check_in=date(2026, 5, 1),
            check_out=date(2026, 5, 3),
            nights=3,
        )
    ok = Reservation(
        id="r1", listing_id="l1", check_in=date(2026, 5, 1), check_out=date(2026, 5, 3), nights=2
    )
    assert ok.guest is None


@pytest.mark.parametrize("prefix", ["check_in", "check_out", "created"])
def test_reservation_query_ranges_validated(prefix: str) -> None:
    with pytest.raises(ValidationError):
        ReservationQuery(**{f"{prefix}_from": date(2026, 5, 3), f"{prefix}_to": date(2026, 5, 1)})


def test_task_query_due_range_validated() -> None:
    with pytest.raises(ValidationError):
        TaskQuery(due_from=date(2026, 5, 3), due_to=date(2026, 5, 1))


def test_write_text_inputs_are_bounded() -> None:
    with pytest.raises(ValidationError):
        ReservationNote(reservation_id="r1", note="")
    with pytest.raises(ValidationError):
        ReviewReply(review_id="v1", body="x" * (MAX_TEXT_LENGTH + 1))
    with pytest.raises(ValidationError):
        OutgoingMessage(conversation_id="c1", body="")
    assert OutgoingMessage(conversation_id="c1", body="hi").channel is MessageChannel.DEFAULT


# ----------------------------------------------------------------- calendar


def test_calendar_day_roundtrip() -> None:
    day = CalendarDay(listing_id="l1", day=date(2026, 7, 1), available=True, price=usd("99"))
    assert CalendarDay.model_validate_json(day.model_dump_json()) == day


def test_calendar_range_bounded() -> None:
    CalendarRange(listing_id="l1", start=date(2026, 1, 1), end=date(2026, 12, 31))
    with pytest.raises(ValidationError):
        CalendarRange(listing_id="l1", start=date(2026, 1, 1), end=date(2027, 1, 2))


def test_calendar_update_rules() -> None:
    with pytest.raises(ValidationError):
        CalendarUpdate(start=TOMORROW, end=TOMORROW)
    with pytest.raises(ValidationError):
        CalendarUpdate(start=TOMORROW, end=TOMORROW + timedelta(days=366), available=False)
    with pytest.raises(ValidationError):
        CalendarUpdate(start=TOMORROW, end=TOMORROW, price=usd("0"))
    with pytest.raises(ValidationError):
        CalendarUpdate(start=TOMORROW, end=TOMORROW, price=usd("-5"))
    with pytest.raises(ValidationError):
        CalendarUpdate(start=TOMORROW, end=TOMORROW, price=usd("10.001"))
    past = date.today() - timedelta(days=10)
    with pytest.raises(ValidationError):
        CalendarUpdate(start=past, end=past, available=False)
    ok = CalendarUpdate(start=TOMORROW, end=TOMORROW, price=usd("149.99"))
    assert ok.price is not None


def test_calendar_update_request_is_capped() -> None:
    update = CalendarUpdate(start=TOMORROW, end=TOMORROW, available=False)
    CalendarUpdateRequest(listing_id="l1", updates=[update])
    with pytest.raises(ValidationError):
        CalendarUpdateRequest(listing_id="l1", updates=[])
    with pytest.raises(ValidationError):
        CalendarUpdateRequest(listing_id="l1", updates=[update] * (MAX_CALENDAR_UPDATES + 1))


# ----------------------------------------------------------------- provider contract


def test_capability_error_message_is_client_safe() -> None:
    err = CapabilityNotSupportedError(Provider.HOSTAWAY, Capability.REVIEWS_REPLY)
    assert err.message == "hostaway does not support 'reviews.reply'."


def test_write_capabilities_are_marked() -> None:
    assert Capability.INBOX_SEND in WRITE_CAPABILITIES
    assert Capability.LISTINGS_READ not in WRITE_CAPABILITIES


async def test_fake_provider_conforms_and_pages() -> None:
    provider = FakeProvider([Listing(id=f"l{i}", title=f"Unit {i}") for i in range(5)])
    assert isinstance(provider, PMSProvider)
    assert as_provider(provider) is provider
    api = require_api(provider, Capability.LISTINGS_READ, provider.listings)
    first = await api.list_listings(ListingQuery(limit=2))
    assert [x.id for x in first.items] == ["l0", "l1"]
    assert first.next_cursor == "2"
    with pytest.raises(PMSValidationError):
        await api.list_listings(ListingQuery(cursor="https://evil.example/next"))


def test_require_api_rejects_unsupported() -> None:
    provider = FakeProvider([])
    with pytest.raises(CapabilityNotSupportedError):
        require_api(provider, Capability.TASKS_READ, provider.tasks)


def test_deadline_helpers() -> None:
    assert remaining_seconds() is None
    token = call_deadline.set(time.monotonic() + 5)
    try:
        remaining = remaining_seconds()
        assert remaining is not None
        assert 0 < remaining <= 5
    finally:
        call_deadline.reset(token)
