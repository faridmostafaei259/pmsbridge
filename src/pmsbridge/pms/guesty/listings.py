"""Guesty listings: list and get, mapped to the canonical `Listing`.

Endpoints (see docs/providers/guesty/listings.md): `GET /listings` and `GET /listings/{id}`.
Guesty's reference is unclear about the shape of the list response (a bare array, or an object with
`results`), so both are accepted. Nothing from the response is copied through except the fields
mapped below: channel connections (`integrations`), credentials and nested objects are never read.
Shared helpers are in `common.py`.
"""

import logging
import re
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import Capability, PMSUpstreamError
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    MAX_SKIP,
    as_dict,
    as_list,
    count_of,
    decode_cursor,
    encode_cursor,
    flag_of,
    listing_id_of,
    number_of,
    strings_of,
    text_of,
)
from pmsbridge.pms.models import (
    UNKNOWN_NAME,
    Address,
    ExtraValue,
    Listing,
    ListingQuery,
    ListingStatus,
    Money,
    Page,
)

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.LISTINGS_READ})

MAX_TAGS: Final = 50
# Only what the mapper reads. Keeping the request small also keeps `integrations` and other
# sensitive parts of a listing out of the response.
LIST_FIELDS: Final = (
    "_id title nickname active isListed isVirtual isTest propertyType roomType address bedrooms "
    "bathrooms accommodates timezone prices defaultCheckInTime defaultCheckOutTime tags terms "
    "cleaningStatus"
)
_CURRENCY_PATTERN: Final = re.compile(r"^[A-Z]{3}$")
MAX_LATITUDE: Final = 90
MAX_LONGITUDE: Final = 180
MAX_BATHROOMS: Final = 1000


# ---- Mapping ----


def _bathrooms(value: object) -> float | None:
    """Guesty's bathroom count (seen live as a number; may be fractional, such as 1.5)."""
    count = number_of(value)
    return float(count) if count is not None and 0 <= count <= MAX_BATHROOMS else None


def _address(raw: object) -> Address | None:
    item = as_dict(raw)
    if item is None:
        return None
    latitude, longitude = number_of(item.get("lat")), number_of(item.get("lng"))
    address = Address(
        line1=text_of(item.get("street")),
        city=text_of(item.get("city")),
        country=text_of(item.get("country")),
        latitude=float(latitude)
        if latitude is not None and abs(latitude) <= MAX_LATITUDE
        else None,
        longitude=(
            float(longitude) if longitude is not None and abs(longitude) <= MAX_LONGITUDE else None
        ),
    )
    return address if address.model_dump(exclude_none=True) else None


def _base_price(raw: object) -> Money | None:
    prices = as_dict(raw)
    if prices is None:
        return None
    amount, currency = number_of(prices.get("basePrice")), text_of(prices.get("currency"))
    if amount is None or amount < 0 or currency is None or not _CURRENCY_PATTERN.match(currency):
        return None
    return Money(amount=Decimal(str(amount)), currency=currency)


def _status(value: object) -> ListingStatus:
    if value is True:
        return ListingStatus.ACTIVE
    if value is False:
        return ListingStatus.INACTIVE
    return ListingStatus.UNKNOWN


def _extras(item: dict[str, object]) -> dict[str, ExtraValue]:
    """A few scalar display values. Never nested objects, channel connections or credentials."""
    terms = as_dict(item.get("terms")) or {}
    status = as_dict(item.get("cleaningStatus")) or {}
    address = as_dict(item.get("address")) or {}
    candidates: dict[str, ExtraValue] = {
        "room_type": text_of(item.get("roomType")),
        "address_full": text_of(address.get("full")),
        "is_listed": flag_of(item.get("isListed")),
        "is_virtual": flag_of(item.get("isVirtual")),
        "is_test": flag_of(item.get("isTest")),
        "min_nights": count_of(terms.get("minNights")),
        "max_nights": count_of(terms.get("maxNights")),
        "cleaning_status": text_of(status.get("value")),
    }
    return {key: value for key, value in candidates.items() if value is not None}


def map_listing(raw: object) -> Listing | None:
    """The canonical listing, or None (logged by id only) for an item that cannot be mapped."""
    item = as_dict(raw)
    listing_id = text_of(item.get("_id")) if item is not None else None
    if item is None or listing_id is None:
        logger.warning("listing skipped: no usable id")
        return None
    try:
        return Listing(
            id=listing_id,
            title=text_of(item.get("title")) or UNKNOWN_NAME,
            nickname=text_of(item.get("nickname")),
            status=_status(item.get("active")),
            property_type=text_of(item.get("propertyType")),
            address=_address(item.get("address")),
            bedrooms=count_of(item.get("bedrooms")),
            bathrooms=_bathrooms(item.get("bathrooms")),
            max_guests=count_of(item.get("accommodates")),
            timezone=text_of(item.get("timezone")),
            base_price=_base_price(item.get("prices")),
            check_in_time=text_of(item.get("defaultCheckInTime")),
            check_out_time=text_of(item.get("defaultCheckOutTime")),
            tags=strings_of(item.get("tags"), limit=MAX_TAGS),
            extras=_extras(item),
        )
    except ValidationError:
        # The error text would repeat the item's values, so only the id is logged.
        logger.warning(
            "listing skipped: could not be mapped", extra={"listing_id": listing_id[:80]}
        )
        return None


def _results(body: object) -> tuple[list[object], int | None]:
    """The listings of a response in either documented shape, and the total if Guesty gave one."""
    items = as_list(body)
    if items is not None:
        return items, None
    envelope = as_dict(body)
    found = as_list(envelope.get("results")) if envelope is not None else None
    if envelope is None or found is None:
        raise PMSUpstreamError("Guesty returned a listings response that could not be understood.")
    return found, count_of(envelope.get("total"))


class GuestyListings:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_listings(self, query: ListingQuery) -> Page[Listing]:
        skip = decode_cursor(query.cursor)
        params: dict[str, str | int] = {
            "limit": query.limit,
            "skip": skip,
            "fields": LIST_FIELDS,
            "sort": "title",
        }
        if query.active_only:
            params["active"] = "true"
        if query.search:
            params["q"] = query.search
        response = await self._client.request("GET", "/listings", params=params)
        raw, total = _results(response.json())
        raw = raw[: query.limit]
        items = [listing for entry in raw if (listing := map_listing(entry)) is not None]
        consumed = skip + len(raw)
        more = consumed < total if total is not None else len(raw) >= query.limit
        next_cursor = encode_cursor(consumed) if more and raw and consumed <= MAX_SKIP else None
        return Page[Listing](items=items, next_cursor=next_cursor, total=total)

    async def get_listing(self, listing_id: str) -> Listing:
        response = await self._client.request(
            "GET", f"/listings/{listing_id_of(listing_id)}", params={"fields": LIST_FIELDS}
        )
        listing = map_listing(response.json())
        if listing is None:
            raise PMSUpstreamError("Guesty returned a listing that could not be understood.")
        return listing


def build(client: GuestyClient) -> GuestyListings:
    return GuestyListings(client)
