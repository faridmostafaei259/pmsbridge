"""Guesty owners (read only): list and get, mapped to the canonical `Owner`.

Endpoints (see docs/providers/guesty/owners.md): `GET /owners` and `GET /owners/{id}`. Only the
fields mapped below are requested. Payout methods, bank accounts, tax ids, addresses, portal
settings and every other owner field are never asked for and the mapper never reads them, even if
Guesty sends them anyway. Names, emails and phones are never logged. Shared helpers are in
`common.py`.
"""

import logging
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
    quote_id,
    text_of,
)
from pmsbridge.pms.models import UNKNOWN_NAME, Owner, Page, PageRequest

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.OWNERS_READ})

# Only what the mapper reads. Keeping the request small also keeps payout and tax details out of
# the response. Guesty documents `fields` as a list but describes it as space separated; one
# space-separated value is understood either way.
OWNER_FIELDS: Final = "_id fullName firstName lastName email phone listings"
MAX_LISTING_IDS: Final = 200


def _listing_ids(value: object) -> list[str]:
    """The listing ids of an owner.

    Guesty documents `listings` as an object but describes it as an array of listing ids, so only
    a list is read: its text items, or the `_id` of its objects. Anything else gives no ids.
    """
    items = as_list(value)
    if items is None:
        return []
    found: list[str] = []
    for item in items:
        entry = as_dict(item)
        listing_id = text_of(entry.get("_id")) if entry is not None else text_of(item)
        if listing_id is not None and listing_id not in found:
            found.append(listing_id)
    return found[:MAX_LISTING_IDS]


def _full_name(item: dict[str, object]) -> str:
    full = text_of(item.get("fullName"))
    if full is not None:
        return full
    parts = [text_of(item.get("firstName")), text_of(item.get("lastName"))]
    return " ".join(part for part in parts if part is not None) or UNKNOWN_NAME


def map_owner(raw: object) -> Owner | None:
    """The canonical owner, or None (logged without any detail) for an unusable item."""
    item = as_dict(raw)
    owner_id = text_of(item.get("_id")) if item is not None else None
    if item is None or owner_id is None:
        logger.warning("owner skipped: no usable id")
        return None
    try:
        return Owner(
            id=owner_id,
            full_name=_full_name(item),
            email=text_of(item.get("email")),
            phone=text_of(item.get("phone")),
            listing_ids=_listing_ids(item.get("listings")),
        )
    except ValidationError:
        # The error text would repeat the owner's values, so only the id is logged.
        logger.warning("owner skipped: could not be mapped", extra={"owner_id": owner_id[:80]})
        return None


def _results(body: object) -> tuple[list[object], int | None]:
    """The owners of a response (a bare list, or `{results, totalCount}`) and the total if given."""
    items = as_list(body)
    if items is not None:
        return items, None
    envelope = as_dict(body)
    found = as_list(envelope.get("results")) if envelope is not None else None
    if envelope is None or found is None:
        raise PMSUpstreamError("Guesty returned an owners response that could not be understood.")
    return found, count_of(envelope.get("totalCount"))


class GuestyOwners:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_owners(self, page: PageRequest) -> Page[Owner]:
        skip = decode_cursor(page.cursor)
        response = await self._client.request(
            "GET",
            "/owners",
            params={
                "fields": OWNER_FIELDS,
                "limit": page.limit,
                "skip": skip,
                "sort": "_id",
                "pagination": "true",
            },
        )
        raw, total = _results(response.json())
        raw = raw[: page.limit]
        items = [owner for entry in raw if (owner := map_owner(entry)) is not None]
        consumed = skip + len(raw)
        more = consumed < total if total is not None else len(raw) >= page.limit
        next_cursor = encode_cursor(consumed) if more and raw and consumed <= MAX_SKIP else None
        return Page[Owner](items=items, next_cursor=next_cursor, total=total)

    async def get_owner(self, owner_id: str) -> Owner:
        response = await self._client.request(
            "GET", f"/owners/{quote_id(owner_id)}", params={"fields": OWNER_FIELDS}
        )
        owner = map_owner(response.json())
        if owner is None:
            raise PMSUpstreamError("Guesty returned an owner that could not be understood.")
        return owner


def build(client: GuestyClient) -> GuestyOwners:
    return GuestyOwners(client)
