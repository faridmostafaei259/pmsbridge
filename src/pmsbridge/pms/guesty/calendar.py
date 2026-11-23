"""Guesty calendar: day-by-day availability and prices, and changes to them.

Endpoints (see docs/providers/guesty/calendar.md): `GET /availability-pricing/api/calendar/listings`
(with `listingIds` set to exactly one listing) to read, and
`PUT /availability-pricing/api/calendar/listings/{id}` to change one date range at a time.

Reading: Guesty's reference does not make clear whether the answer is a plain list of days or an
object around them, so both are accepted. A day is available when Guesty gives a unit count
(`allotment`) above zero, or, without a count, when its `status` is `available`. A day without
either is shown as not available: a day is never reported free unless Guesty says so. Staff text on
a block (`blockRefs`) is never read.

Changing: checks run before anything is sent. The calendar must be read completely for every day
of every range; a price must be in the listing's currency (from `GET /listings/{id}`), near the
day's own current price and near the listing's base price (so repeated changes cannot ratchet a
price far away); availability cannot change on a day with a reservation or on a multi-unit day.
Each range is one `PUT`; the shared client never resends one except after a 429 with `Retry-After`.
If a later range fails, the error says exactly which ranges were applied.
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import (
    Capability,
    PMSAuthError,
    PMSError,
    PMSNotFoundError,
    PMSPermissionError,
    PMSRateLimitedError,
    PMSTokenBudgetError,
    PMSUpstreamError,
    PMSValidationError,
    remaining_seconds,
)
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    as_dict,
    as_list,
    count_of,
    date_of,
    money_of,
    number_of,
    quote_id,
    text_of,
)
from pmsbridge.pms.models import (
    MAX_CALENDAR_RANGE_DAYS,
    CalendarDay,
    CalendarRange,
    CalendarUpdate,
    CalendarUpdateRequest,
    ExtraValue,
)

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.CALENDAR_READ, Capability.CALENDAR_WRITE})

READ_PATH: Final = "/availability-pricing/api/calendar/listings"
LOW_PRICE_FACTOR: Final = Decimal("0.25")
HIGH_PRICE_FACTOR: Final = 4
MIN_SECONDS_PER_WRITE: Final = 2.0
LISTING_FIELDS: Final = "_id prices"
_BOOKED_STATUSES: Final = frozenset({"booked", "reserved"})
_CURRENCY_PATTERN: Final = re.compile(r"^[A-Z]{3}$")
_MAX_ENVELOPE_DEPTH: Final = 2
_MISTAKE_HINT: Final = "If the price is intended, change it in Guesty directly."


# ---- Mapping ----


def _unwrap(body: object, depth: int = 0) -> tuple[list[object], str | None]:
    """The day objects (a bare list or an object around it) and the envelope currency."""
    items = as_list(body)
    if items is not None:
        return items, None
    envelope = as_dict(body)
    if envelope is not None and depth <= _MAX_ENVELOPE_DEPTH:
        outer = text_of(envelope.get("currency"))
        for key in ("data", "days", "calendar", "results"):
            if key in envelope:
                found, inner = _unwrap(envelope[key], depth + 1)
                return found, inner or outer
    raise PMSUpstreamError("Guesty returned a calendar that could not be understood.")


def _blocked_reason(blocks: object) -> str | None:
    """Guesty does not say what its block keys mean, so any true one is just 'blocked'."""
    flags = as_dict(blocks)
    if flags is not None and any(value is True for value in flags.values()):
        return "blocked"
    return None


def map_day(raw: object, listing_id: str, envelope_currency: str | None) -> CalendarDay | None:
    """A canonical day, or None for an item that cannot be read as a day."""
    item = as_dict(raw)
    day = date_of(item.get("date")) if item is not None else None
    if item is None or day is None:
        logger.warning("calendar day skipped: no usable date")
        return None
    other = text_of(item.get("listingId"))
    if other is not None and other != listing_id:
        return None
    status = text_of(item.get("status"))
    status = status.lower() if status is not None else None
    allotment = number_of(item.get("allotment"))
    available = allotment > 0 if allotment is not None else status == "available"
    reservation_id = text_of(item.get("reservationId"))
    booked = (
        reservation_id is not None
        or status in _BOOKED_STATUSES
        or bool(as_dict(item.get("reservation")))
    )
    currency = text_of(item.get("currency")) or envelope_currency
    price = money_of(item.get("price"), currency)
    extras: dict[str, ExtraValue] = {}
    if status is not None:
        extras["pms_status"] = status
    if allotment is not None:
        extras["allotment"] = allotment
    if booked:
        extras["booked"] = True
    try:
        mapped = CalendarDay(
            listing_id=listing_id,
            day=day,
            available=available,
            price=price,
            min_nights=count_of(item.get("minNights")),
            reservation_id=reservation_id,
            blocked_reason=None if available or booked else _blocked_reason(item.get("blocks")),
            extras=extras,
        )
    except ValidationError:
        logger.warning("calendar day skipped: could not be mapped")
        return None
    return mapped


def map_calendar(body: object, listing_id: str, start: date, end: date) -> list[CalendarDay]:
    """The days inside [start, end] in date order, one per date.

    When a date appears more than once, the first entry is kept, unless a later one has a
    reservation and the first does not: a date counts as booked if any entry for it is booked.
    """
    items, envelope_currency = _unwrap(body)
    found: dict[date, CalendarDay] = {}
    for raw in items[: 2 * MAX_CALENDAR_RANGE_DAYS]:
        mapped = map_day(raw, listing_id, envelope_currency)
        if mapped is None or not start <= mapped.day <= end:
            continue
        kept = found.get(mapped.day)
        if kept is None or (_has_reservation(mapped) and not _has_reservation(kept)):
            found[mapped.day] = mapped
    return [found[day] for day in sorted(found)]


# ---- Safety checks ----


def _days_in(days: list[CalendarDay], update: CalendarUpdate) -> list[CalendarDay]:
    return [day for day in days if update.start <= day.day <= update.end]


def _has_reservation(day: CalendarDay) -> bool:
    return day.reservation_id is not None or day.extras.get("booked") is True


def check_not_past(updates: list[CalendarUpdate], today: date) -> None:
    """Refuse a range that starts before yesterday (one day of tolerance for time zones)."""
    for number, update in enumerate(updates, start=1):
        if update.start < today - timedelta(days=1):
            raise PMSValidationError(
                f"Range {number} starts on {update.start}, before yesterday; days in the past "
                "cannot be changed. Nothing was changed."
            )


def check_updates(
    updates: list[CalendarUpdate],
    days: list[CalendarDay],
    currency: str | None,
    base_price: Decimal | None,
) -> None:
    """Refuse, before anything is sent, a change that is probably a mistake or cannot be checked.

    `currency` and `base_price` are the listing's own (from the listing record); they are only
    needed, and only fetched, when a range sets a price.
    """
    present = {day.day for day in days}
    for number, update in enumerate(updates, start=1):
        span = f"{update.start} to {update.end}"
        wanted = {
            update.start + timedelta(days=n) for n in range((update.end - update.start).days + 1)
        }
        if not wanted <= present:
            raise PMSValidationError(
                f"Range {number} ({span}) was not sent: Guesty's calendar does not show every "
                "day in it, so the current prices and bookings cannot be checked. Nothing was "
                "changed."
            )
        within = _days_in(days, update)
        if update.price is not None:
            _check_price(update, number, span, within, currency=currency, base_price=base_price)
        if update.available is not None:
            if any(_has_reservation(day) for day in within):
                raise PMSValidationError(
                    f"Range {number}: availability cannot be changed for {span}, because at least "
                    "one of these days has a reservation. Nothing was changed. Change it in "
                    "Guesty directly if it is intended."
                )
            if any("allotment" in day.extras for day in within):
                raise PMSValidationError(
                    f"Range {number}: availability cannot be changed for {span}, because the "
                    "listing has several units and the change would not be reliable. Nothing was "
                    "changed. Change it in Guesty directly."
                )


def _check_price(
    update: CalendarUpdate,
    number: int,
    span: str,
    within: list[CalendarDay],
    *,
    currency: str | None,
    base_price: Decimal | None,
) -> None:
    assert update.price is not None  # noqa: S101  (the caller checked)
    if currency is None:
        raise PMSValidationError(
            f"Range {number}: Guesty did not say which currency this listing uses, so the price "
            "was not sent. Nothing was changed."
        )
    if update.price.currency != currency:
        raise PMSValidationError(
            f"Range {number}: the price for {span} is in {update.price.currency}, but this "
            f"listing uses {currency}. Nothing was changed."
        )
    current = [day.price.amount for day in within if day.price and day.price.currency == currency]
    if len(current) != len(within):
        raise PMSValidationError(
            f"Range {number} ({span}) was not sent: Guesty's calendar shows no price in "
            f"{currency} for some of these days, so the change cannot be checked. Nothing was "
            "changed."
        )
    amount = update.price.amount
    if base_price is None:
        raise PMSValidationError(
            f"Range {number}: this listing has no base price in Guesty, so the price cannot be "
            "checked and was not sent. Nothing was changed."
        )
    if amount < base_price * LOW_PRICE_FACTOR or amount > base_price * HIGH_PRICE_FACTOR:
        raise PMSValidationError(
            f"Range {number}: this price looks like a mistake: {amount} {currency} is far from "
            f"the listing's base price of {base_price} {currency} (allowed from "
            f"{base_price * LOW_PRICE_FACTOR} to {base_price * HIGH_PRICE_FACTOR}). Nothing was "
            f"changed. {_MISTAKE_HINT}"
        )
    # No day may drop below a quarter, or rise above four times, its own current price.
    low, high = min(current), max(current)
    if amount < high * LOW_PRICE_FACTOR or amount > low * HIGH_PRICE_FACTOR:
        raise PMSValidationError(
            f"Range {number}: this price looks like a mistake: {amount} for {span}, while the "
            f"current nightly prices there run from {low} to {high} {currency}. Nothing was "
            f"changed. {_MISTAKE_HINT}"
        )


# ---- Partial failure ----

# What to say when Guesty did not apply a range (the request was refused or never sent).
_NOT_APPLIED: Final[dict[type[PMSError], str]] = {
    PMSValidationError: "Guesty rejected it as invalid",
    PMSNotFoundError: "Guesty could not find the listing",
    PMSPermissionError: "Guesty denied it (the API key may not allow it)",
    PMSAuthError: "Guesty did not accept the access token",
    PMSRateLimitedError: "Guesty's rate limit was reached",
    PMSTokenBudgetError: "no Guesty token can be requested right now",
}


def _numbers(first: int, last: int, verb: tuple[str, str]) -> str:
    if first == last:
        return f"Range {first} {verb[0]}."
    return f"Ranges {first} to {last} {verb[1]}."


def _applied(number: int) -> str:
    """What was applied before range `number`."""
    if number > 1:
        return _numbers(1, number - 1, ("was applied", "were applied"))
    return "Nothing was changed."


def partial_failure(exc: PMSError, number: int, total: int, update: CalendarUpdate) -> PMSError:
    """An error of the same kind that says exactly which ranges were applied and which were not.

    A failure that leaves the outcome open (a timeout, a dropped connection, a server error) is
    always reported as unknown, because the change may have been made.
    """
    ref = f"Range {number} of {total} ({update.start} to {update.end})"
    reason = _NOT_APPLIED.get(type(exc))
    if reason is None:
        parts = [f"{ref} did not complete and may or may not have been applied."]
    else:
        parts = [f"{ref} was not applied: {reason}."]
    if number > 1 or reason is not None:
        parts.append(_applied(number))
    if number < total:
        parts.append(_numbers(number + 1, total, ("was not sent", "were not sent")))
    if reason is None:
        parts.append(
            f"Check the calendar before trying again: the outcome of range {number} is unknown."
        )
    message = " ".join(parts)
    if isinstance(exc, PMSRateLimitedError):
        return PMSRateLimitedError(message, exc.retry_after_seconds)
    if reason is None:
        return PMSUpstreamError(message)
    return type(exc)(message)


def out_of_time(number: int) -> PMSUpstreamError:
    """Range `number` and the ones after it were not sent because the deadline is too close."""
    return PMSUpstreamError(
        f"Range {number} and later were not sent: the time allowed ran out. {_applied(number)}"
        + (" Check the calendar before trying again." if number > 1 else "")
    )


# ---- Adapter ----


class GuestyCalendar:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def _read(self, listing_id: str, start: date, end: date) -> list[CalendarDay]:
        response = await self._client.request(
            "GET",
            READ_PATH,
            params={
                "listingIds": listing_id,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            },
        )
        return map_calendar(response.json(), listing_id, start, end)

    @staticmethod
    def _checked_id(listing_id: str) -> str:
        wanted = listing_id.strip()
        quote_id(wanted)  # length and emptiness check; the id goes in as a query value for reads
        if "," in wanted:
            # Guesty reads a comma as a list of ids, and exactly one is wanted.
            raise PMSValidationError("The id is not valid.")
        return wanted

    async def get_calendar(self, calendar_range: CalendarRange) -> list[CalendarDay]:
        wanted = self._checked_id(calendar_range.listing_id)
        return await self._read(wanted, calendar_range.start, calendar_range.end)

    async def _listing_prices(self, listing_id: str) -> tuple[str | None, Decimal | None]:
        """The listing's own currency and base price (the anchor for the price check)."""
        response = await self._client.request(
            "GET", f"/listings/{quote_id(listing_id)}", params={"fields": LISTING_FIELDS}
        )
        prices = as_dict((as_dict(response.json()) or {}).get("prices")) or {}
        code = (text_of(prices.get("currency")) or "").upper()
        base = number_of(prices.get("basePrice"))
        return (
            code if _CURRENCY_PATTERN.match(code) else None,
            Decimal(str(base)) if base is not None and base > 0 else None,
        )

    async def update_calendar(self, request: CalendarUpdateRequest) -> list[CalendarDay]:
        wanted = self._checked_id(request.listing_id)
        updates = request.updates
        first = min(update.start for update in updates)
        last = max(update.end for update in updates)
        if (last - first).days >= MAX_CALENDAR_RANGE_DAYS:
            raise PMSValidationError(
                f"The updates span more than {MAX_CALENDAR_RANGE_DAYS} days from the first date "
                "to the last. Send them in separate requests. Nothing was changed."
            )
        check_not_past(updates, date.today())
        currency, base_price = None, None
        if any(update.price is not None for update in updates):
            currency, base_price = await self._listing_prices(wanted)
        before = await self._read(wanted, first, last)
        check_updates(updates, before, currency, base_price)
        path = f"{READ_PATH}/{quote_id(wanted)}"
        for number, update in enumerate(updates, start=1):
            left = remaining_seconds()
            if left is not None and left < MIN_SECONDS_PER_WRITE:
                raise out_of_time(number)
            try:
                await self._client.request("PUT", path, json_body=_body(update))
            except PMSError as exc:
                logger.warning(
                    "calendar update stopped",
                    extra={
                        "listing_id": wanted[:80],
                        "update": number,
                        "of": len(updates),
                        "error_class": type(exc).__name__,
                    },
                )
                raise partial_failure(exc, number, len(updates), update) from None
        logger.info(
            "calendar updated",
            extra={"listing_id": wanted[:80], "updates": len(updates)},
        )
        try:
            after = await self._read(wanted, first, last)
        except PMSError:
            raise PMSUpstreamError(
                "The changes were applied but the updated calendar could not be read; "
                "check it in Guesty."
            ) from None
        return [day for day in after if any(_days_in([day], update) for update in updates)]


def _body(update: CalendarUpdate) -> dict[str, str | int | float]:
    """The request body: the dates and only the fields that were set."""
    body: dict[str, str | int | float] = {
        "startDate": update.start.isoformat(),
        "endDate": update.end.isoformat(),
    }
    if update.price is not None:
        body["price"] = float(update.price.amount)
    if update.min_nights is not None:
        body["minNights"] = update.min_nights
    if update.available is not None:
        body["status"] = "available" if update.available else "unavailable"
        if not update.available:
            body["blockReason"] = "Other"
    return body


def build(client: GuestyClient) -> GuestyCalendar:
    return GuestyCalendar(client)
