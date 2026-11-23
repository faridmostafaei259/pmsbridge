"""Guesty reservations (read only): list and get, mapped to the canonical `Reservation`.

Endpoints (see docs/providers/guesty/reservations.md): `GET /reservations-v3/search` for lists,
`GET /reservations-v3?reservationIds=` for one reservation (Guesty has no get-by-id path), then,
best effort, `GET /guests-crud/{guestId}` for the guest's contact details and
`GET /reservations-reports/reservations/search` for the balance still owed.

Nothing is copied through except the fields mapped below. Door and lock codes (`notes.keyCode`),
the author of a note (`notes.doneBy`), payment methods, cards, payouts and channel metadata are
never read. Guest names, contact details and notes are never logged. Shared helpers are in
`common.py`.
"""

import logging
from datetime import date, timedelta
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import (
    Capability,
    CapabilityNotSupportedError,
    PMSError,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
    Provider,
)
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    MAX_SKIP,
    as_dict,
    as_list,
    count_of,
    date_of,
    datetime_of,
    decode_cursor,
    encode_cursor,
    flag_of,
    money_of,
    quote_id,
    text_of,
)
from pmsbridge.pms.models import (
    UNKNOWN_NAME,
    ExtraValue,
    GuestSummary,
    Money,
    Page,
    Reservation,
    ReservationNote,
    ReservationQuery,
    ReservationStatus,
)

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.RESERVATIONS_READ})

# Guesty status -> canonical status. Anything else is UNKNOWN (the raw value goes to extras).
STATUS_FROM_GUESTY: Final[dict[str, ReservationStatus]] = {
    "inquiry": ReservationStatus.INQUIRY,
    "awaiting_payment": ReservationStatus.PENDING,
    "reserved": ReservationStatus.PENDING,
    "confirmed": ReservationStatus.CONFIRMED,
    "checked_in": ReservationStatus.CHECKED_IN,
    "checked_out": ReservationStatus.CHECKED_OUT,
    "completed": ReservationStatus.CHECKED_OUT,
    "canceled": ReservationStatus.CANCELLED,
    "declined": ReservationStatus.DECLINED,
    "expired": ReservationStatus.EXPIRED,
}
# Canonical status -> the Guesty statuses to ask for, in a stable order.
STATUS_TO_GUESTY: Final[dict[ReservationStatus, tuple[str, ...]]] = {
    ReservationStatus.INQUIRY: ("inquiry",),
    ReservationStatus.PENDING: ("awaiting_payment", "reserved"),
    ReservationStatus.CONFIRMED: ("confirmed",),
    ReservationStatus.CHECKED_IN: ("checked_in",),
    ReservationStatus.CHECKED_OUT: ("checked_out", "completed"),
    ReservationStatus.CANCELLED: ("canceled",),
    ReservationStatus.DECLINED: ("declined",),
    ReservationStatus.EXPIRED: ("expired",),
    ReservationStatus.UNKNOWN: ("closed", "alteration"),
}

SEARCH_PATH: Final = "/reservations-v3/search"
BY_IDS_PATH: Final = "/reservations-v3"
REPORTS_PATH: Final = "/reservations-reports/reservations/search"
GUEST_FIELDS: Final = "fullName email phone"
REPORT_LIMIT: Final = 10
MAX_STAYS: Final = 50
# Notes keys that are shown, in this order, with their label. `keyCode` and `doneBy` are not here
# on purpose: door codes and staff names are never read.
NOTE_LABELS: Final = (
    ("guest", "Guest note"),
    ("specialRequests", "Special requests"),
    ("other", "Staff note"),
    ("cleaning", "Cleaning note"),
)


# ---- Mapping ----


def _status(value: object) -> ReservationStatus:
    text = text_of(value)
    return (
        STATUS_FROM_GUESTY.get(text.lower(), ReservationStatus.UNKNOWN)
        if text
        else (ReservationStatus.UNKNOWN)
    )


def _stays(item: dict[str, object]) -> list[dict[str, object]]:
    raw = as_list(item.get("stay")) or []
    return [stay for entry in raw[:MAX_STAYS] if (stay := as_dict(entry)) is not None]


def _listing_id(stays: list[dict[str, object]]) -> str | None:
    """The first stay's listing. A by-ids result names it `unitId` (or `unitTypeId`)."""
    if not stays:
        return None
    first = stays[0]
    for key in ("listingId", "unitId", "unitTypeId"):
        found = text_of(first.get(key))
        if found is not None:
            return found
    return None


def _dates(item: dict[str, object], stays: list[dict[str, object]]) -> tuple[date, date] | None:
    """Local check-in and check-out dates: the reservation's own, else the span of its stays."""
    check_in, check_out = (
        date_of(item.get("checkInDateLocalized")),
        date_of(item.get("checkOutDateLocalized")),
    )
    if check_in is None:
        starts = [d for s in stays if (d := date_of(s.get("checkInDateLocalized"))) is not None]
        check_in = min(starts) if starts else None
    if check_out is None:
        ends = [d for s in stays if (d := date_of(s.get("checkOutDateLocalized"))) is not None]
        check_out = max(ends) if ends else None
    if check_in is None or check_out is None:
        return None
    return check_in, check_out


def _guests_count(item: dict[str, object], stays: list[dict[str, object]]) -> int | None:
    """The largest party over the stays (stays of one booking share the guests)."""
    counts = [c for s in stays if (c := count_of(s.get("guestsCount"))) is not None]
    top = count_of(item.get("guestsCount"))
    if top is not None:
        counts.append(top)
    return max(counts) if counts else None


def notes_text(raw: object) -> str | None:
    """The shown notes as labelled lines. Door codes (`keyCode`) and `doneBy` are never read."""
    notes = as_dict(raw)
    if notes is None:
        return None
    lines = [f"{label}: {text}" for key, label in NOTE_LABELS if (text := text_of(notes.get(key)))]
    return "\n".join(lines) or None


def _extras(item: dict[str, object], status: str | None) -> dict[str, ExtraValue]:
    stay_status = as_dict(item.get("guestStay")) or {}
    candidates: dict[str, ExtraValue] = {
        "pms_status": status,
        "source": text_of(item.get("source")),
        "guest_stay_status": text_of(stay_status.get("status")),
    }
    return {key: value for key, value in candidates.items() if value is not None}


def map_reservation(raw: object) -> Reservation | None:
    """The canonical reservation, or None (logged by id only) for an item that cannot be mapped.

    Both result shapes are accepted: a search result (`reservationId`, `guestId`, `channel`) and a
    by-ids result (`_id`, `bookerId`, `platform`). Guest contact and money are not in either.
    """
    item = as_dict(raw)
    reservation_id = _id_of(item)
    if item is None or reservation_id is None:
        logger.warning("reservation skipped: no usable id")
        return None
    stays = _stays(item)
    listing_id, dates = _listing_id(stays), _dates(item, stays)
    if listing_id is None or dates is None:
        logger.warning(
            "reservation skipped: no listing or dates",
            extra={"reservation_id": reservation_id[:80]},
        )
        return None
    check_in, check_out = dates
    status = text_of(item.get("status"))
    try:
        return Reservation(
            id=reservation_id,
            listing_id=listing_id,
            status=_status(status),
            channel=text_of(item.get("channel")) or text_of(item.get("platform")),
            confirmation_code=text_of(item.get("confirmationCode")),
            guest_id=text_of(item.get("guestId")) or text_of(item.get("bookerId")),
            guest=None,
            check_in=check_in,
            check_out=check_out,
            nights=(check_out - check_in).days,
            guests_count=_guests_count(item, stays),
            total_price=None,
            balance_due=None,
            created_at=datetime_of(item.get("createdAt"), naive_as_utc=True),  # documented as UTC
            notes=notes_text(item.get("notes")),
            extras=_extras(item, status),
        )
    except ValidationError:
        # The error text would repeat the item's values, so only the id is logged.
        logger.warning(
            "reservation skipped: could not be mapped",
            extra={"reservation_id": reservation_id[:80]},
        )
        return None


def _id_of(item: dict[str, object] | None) -> str | None:
    """A search result names its id `reservationId`; a by-ids result names it `_id`."""
    if item is None:
        return None
    return text_of(item.get("reservationId")) or text_of(item.get("_id"))


def _results(body: object) -> tuple[list[object], bool | None]:
    """The items of a response (a bare array or `results`), and `pagination.hasMore` if given."""
    items = as_list(body)
    if items is not None:
        return items, None
    envelope = as_dict(body)
    found = as_list(envelope.get("results")) if envelope is not None else None
    if envelope is None or found is None:
        raise PMSUpstreamError(
            "Guesty returned a reservations response that could not be understood."
        )
    return found, flag_of((as_dict(envelope.get("pagination")) or {}).get("hasMore"))


# ---- Filters ----


def _start_of_day(day: date) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"


def build_filters(query: ReservationQuery) -> dict[str, str | int]:
    """The Guesty search filters for a query (only the ones the query sets)."""
    params: dict[str, str | int] = {}
    if query.listing_id:
        params["filter[listingId]"] = query.listing_id
    if query.statuses:
        wanted: list[str] = []
        for status in query.statuses:
            wanted.extend(name for name in STATUS_TO_GUESTY[status] if name not in wanted)
        params["filter[status]"] = ",".join(wanted)
    for name, start, end in (
        ("checkIn", query.check_in_from, query.check_in_to),
        ("checkOut", query.check_out_from, query.check_out_to),
    ):
        if start:
            params[f"filter[{name}][gte]"] = start.isoformat()
        if end:
            params[f"filter[{name}][lte]"] = end.isoformat()
    # The last day counts in full, so the upper bound is the start of the next day.
    if query.created_from:
        params["filter[createdAt][gte]"] = _start_of_day(query.created_from)
    if query.created_to:
        params["filter[createdAt][lt]"] = _start_of_day(query.created_to + timedelta(days=1))
    if query.search:
        params["filter[confirmationCode]"] = query.search
    return params


# ---- Adapter ----


class GuestyReservations:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_reservations(self, query: ReservationQuery) -> Page[Reservation]:
        skip = decode_cursor(query.cursor)
        params: dict[str, str | int] = {
            **build_filters(query),
            "sort": "checkIn",
            "limit": query.limit,
            "skip": skip,
        }
        response = await self._client.request("GET", SEARCH_PATH, params=params)
        raw, has_more = _results(response.json())
        raw = raw[: query.limit]
        items = [found for entry in raw if (found := map_reservation(entry)) is not None]
        consumed = skip + len(raw)
        more = has_more if has_more is not None else len(raw) >= query.limit
        next_cursor = encode_cursor(consumed) if more and raw and consumed <= MAX_SKIP else None
        return Page[Reservation](items=items, next_cursor=next_cursor, total=None)

    async def get_reservation(self, reservation_id: str) -> Reservation:
        wanted = reservation_id.strip()
        quote_id(wanted)  # length and emptiness check; the id itself goes in as a query value
        if "," in wanted:
            # Guesty reads a comma as a list of ids, and exactly one is wanted.
            raise PMSValidationError("The id is not valid.")
        response = await self._client.request("GET", BY_IDS_PATH, params={"reservationIds": wanted})
        found, _ = _results(response.json())  # a bare array, or an object with `results`
        raw = next((e for e in found if _id_of(as_dict(e)) == wanted), None)
        if raw is None:
            raise PMSNotFoundError("Reservation not found.")
        reservation = map_reservation(raw)
        if reservation is None:
            raise PMSUpstreamError("Guesty returned a reservation that could not be understood.")
        guest = await self._guest(wanted, reservation.guest_id)
        balance = await self._balance(wanted, reservation.confirmation_code)
        return reservation.model_copy(update={"guest": guest, "balance_due": balance})

    async def add_reservation_note(self, note: ReservationNote) -> Reservation:
        raise CapabilityNotSupportedError(Provider.GUESTY, Capability.RESERVATIONS_ADD_NOTE)

    async def _guest(self, reservation_id: str, guest_id: str | None) -> GuestSummary | None:
        """Name, email and phone from the guest record; None when it cannot be had."""
        if guest_id is None:
            return None
        try:
            response = await self._client.request(
                "GET", f"/guests-crud/{quote_id(guest_id)}", params={"fields": GUEST_FIELDS}
            )
            item = as_dict(response.json())
            if item is None:
                return None
            name, email, phone = (text_of(item.get(key)) for key in ("fullName", "email", "phone"))
            if name is None and email is None and phone is None:
                return None
            return GuestSummary(name=name or UNKNOWN_NAME, email=email, phone=phone)
        except (PMSError, ValidationError) as exc:
            _skipped("guest contact", reservation_id, exc)
            return None

    async def _balance(self, reservation_id: str, confirmation_code: str | None) -> Money | None:
        """The balance still owed, from the reservations report; None when it cannot be had."""
        if confirmation_code is None:
            return None
        try:
            response = await self._client.request(
                "GET",
                REPORTS_PATH,
                params={"filter[confirmationCode]": confirmation_code, "limit": REPORT_LIMIT},
            )
            raw, _ = _results(response.json())
            for entry in raw:
                row = as_dict(entry)
                if row is None or text_of(row.get("reservationId")) != reservation_id:
                    continue
                money = as_dict(row.get("money")) or {}
                return money_of(money.get("balanceDue"), money.get("currency"))
            return None
        except (PMSError, ValidationError) as exc:
            _skipped("balance", reservation_id, exc)
            return None


def _skipped(what: str, reservation_id: str, exc: Exception) -> None:
    """Log that an optional part was left out. Only the class of the error is recorded."""
    logger.warning(
        "reservation %s left out",
        what,
        extra={"reservation_id": reservation_id[:80], "error_class": type(exc).__name__},
    )


def build(client: GuestyClient) -> GuestyReservations:
    return GuestyReservations(client)
