"""Guesty guests (read only): list and get, mapped to the canonical `Guest`.

Endpoints (see docs/providers/guesty/guests.md): `GET /guests-crud` and `GET /guests-crud/{id}`.
Only the fields mapped below are requested. Identity and passport numbers, birthdays, addresses,
staff notes, payment methods and the lists of extra emails and phones are never asked for and never
read. Guesty cannot search guests by text, so a search is refused before any request is made.
Names, emails and phones are never logged. Shared helpers are in `common.py`.
"""

import logging
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import Capability, PMSUpstreamError, PMSValidationError
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    MAX_SKIP,
    as_dict,
    as_list,
    count_of,
    decode_cursor,
    encode_cursor,
    flag_of,
    quote_id,
    text_of,
)
from pmsbridge.pms.models import UNKNOWN_NAME, ExtraValue, Guest, GuestQuery, Page

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.GUESTS_READ})

# Only what the mapper reads. Keeping the request small also keeps identity documents, birthdays,
# addresses and staff notes out of the response.
GUEST_FIELDS: Final = "_id firstName lastName fullName email phone preferredLanguage returningGuest"
SEARCH_REFUSED: Final = (
    "This system cannot search guests by name or email. Find the guest through their reservation "
    "(get_reservation) or list guests page by page."
)


def _language(value: object) -> str | None:
    """Guesty documents a string but its example is a list: take the first string."""
    found = text_of(value)
    if found is not None:
        return found
    items = as_list(value)
    if items is None:
        return None
    return next((text for item in items if (text := text_of(item)) is not None), None)


def map_guest(raw: object) -> Guest | None:
    """The canonical guest, or None (logged without any detail) for an unusable item."""
    item = as_dict(raw)
    guest_id = (text_of(item.get("_id")) or text_of(item.get("id"))) if item is not None else None
    if item is None or guest_id is None:
        logger.warning("guest skipped: no usable id")
        return None
    returning = flag_of(item.get("returningGuest"))
    extras: dict[str, ExtraValue] = {} if returning is None else {"returning_guest": returning}
    try:
        return Guest(
            id=guest_id,
            full_name=text_of(item.get("fullName")) or UNKNOWN_NAME,
            first_name=text_of(item.get("firstName")),
            last_name=text_of(item.get("lastName")),
            email=text_of(item.get("email")) or text_of(item.get("guestEmail")),
            phone=text_of(item.get("phone")) or text_of(item.get("guestPhone")),
            language=_language(item.get("preferredLanguage")),
            extras=extras,
        )
    except ValidationError:
        # The error text would repeat the guest's values, so only the id is logged.
        logger.warning("guest skipped: could not be mapped", extra={"guest_id": guest_id[:80]})
        return None


def _results(body: object) -> tuple[list[object], int | None]:
    """The guests of a response (a bare list or `{results, total}`) and the total if given."""
    items = as_list(body)
    if items is not None:
        return items, None
    envelope = as_dict(body)
    found = as_list(envelope.get("results")) if envelope is not None else None
    if envelope is None or found is None:
        raise PMSUpstreamError("Guesty returned a guests response that could not be understood.")
    return found, count_of(envelope.get("total"))


class GuestyGuests:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_guests(self, query: GuestQuery) -> Page[Guest]:
        if query.search:
            raise PMSValidationError(SEARCH_REFUSED)
        skip = decode_cursor(query.cursor)
        response = await self._client.request(
            "GET",
            "/guests-crud",
            params={"columns": GUEST_FIELDS, "limit": query.limit, "skip": skip},
        )
        raw, total = _results(response.json())
        raw = raw[: query.limit]
        items = [guest for entry in raw if (guest := map_guest(entry)) is not None]
        consumed = skip + len(raw)
        more = consumed < total if total is not None else len(raw) >= query.limit
        next_cursor = encode_cursor(consumed) if more and raw and consumed <= MAX_SKIP else None
        return Page[Guest](items=items, next_cursor=next_cursor, total=total)

    async def get_guest(self, guest_id: str) -> Guest:
        response = await self._client.request(
            "GET", f"/guests-crud/{quote_id(guest_id)}", params={"fields": GUEST_FIELDS}
        )
        guest = map_guest(response.json())
        if guest is None:
            raise PMSUpstreamError("Guesty returned a guest that could not be understood.")
        return guest


def build(client: GuestyClient) -> GuestyGuests:
    return GuestyGuests(client)
