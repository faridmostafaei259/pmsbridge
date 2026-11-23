"""Guesty listing content: full details and photos (read only).

Endpoints (see docs/providers/guesty/listings.md): `GET /listings/{id}` for details and
`GET /properties-api/property-photos/property-photos/{id}` for photos. Photo changes are not
available yet: those methods refuse with `CapabilityNotSupportedError`, and `PHOTOS_WRITE` is not
in `CAPABILITIES`.

Guesty does not document the keys inside `publicDescription`, so every text value in it is read and
the key decides the kind when it is a known one. Staff-only text (`privateDescription`) is never
requested or read. Rooms and house rules have no field on Guesty's listing record, so they stay
empty here.
"""

import logging
import re
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import (
    Capability,
    CapabilityNotSupportedError,
    PMSUpstreamError,
    Provider,
)
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    as_dict,
    as_list,
    count_of,
    number_of,
    quote_id,
    strings_of,
    text_of,
)
from pmsbridge.pms.models import (
    MAX_EXTRAS_KEYS,
    MAX_PHOTOS,
    DescriptionKind,
    ExtraValue,
    ListingDescription,
    ListingDetails,
    ListingPhoto,
    PhotoCaptionUpdate,
    PhotoDelete,
    PhotoOrder,
)

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.LISTING_DETAILS_READ, Capability.PHOTOS_READ})

# `privateDescription` (staff-only text) is deliberately never requested.
DETAIL_FIELDS: Final = (
    "_id publicDescription amenities amenitiesNotIncluded checkInInstructions customFields"
)
MAX_AMENITIES: Final = 200
MAX_DESCRIPTIONS: Final = 20
MAX_CUSTOM_KEY_LENGTH: Final = 100
_KIND_BY_KEY: Final = {
    "summary": DescriptionKind.SUMMARY,
    "space": DescriptionKind.SPACE,
    "access": DescriptionKind.ACCESS,
    "neighborhood": DescriptionKind.NEIGHBORHOOD,
    "transit": DescriptionKind.TRANSIT,
    "interaction": DescriptionKind.INTERACTION,
    "interactionwithguests": DescriptionKind.INTERACTION,
    "notes": DescriptionKind.NOTES,
    "houserules": DescriptionKind.HOUSE_RULES,
}
_CHECK_IN_METHODS: Final = {
    "RECEPTION": "reception",
    "LOCK_BOX": "lock box",
    "DOOR_CODE": "door code",
    "MEET": "meet the host",
    "OTHER": "other",
}
_URL_PATTERN: Final = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _descriptions(item: dict[str, object]) -> list[ListingDescription]:
    found: list[ListingDescription] = []
    public = item.get("publicDescription")
    if (single := text_of(public)) is not None:
        found.append(ListingDescription(kind=DescriptionKind.SUMMARY, text=single))
    for key, value in (as_dict(public) or {}).items():
        if (body := text_of(value)) is not None:
            normalized = re.sub(r"[^a-z]", "", key.lower())
            kind = _KIND_BY_KEY.get(normalized, DescriptionKind.OTHER)
            found.append(ListingDescription(kind=kind, text=body))
    return found[:MAX_DESCRIPTIONS]


def _check_in_instructions(raw: object) -> str | None:
    instructions = as_dict(raw)
    if instructions is None:
        return None
    parts: list[str] = []
    for label, key in (
        ("Check-in method", "primaryCheckIn"),
        ("Alternative", "alternativeCheckIn"),
    ):
        method = text_of(instructions.get(key))
        if method is not None:
            parts.append(f"{label}: {_CHECK_IN_METHODS.get(method.upper(), method.lower())}.")
    if (notes := text_of(instructions.get("notes"))) is not None:
        parts.append(notes)
    return " ".join(parts) or None


def _custom_fields(raw: object) -> dict[str, ExtraValue]:
    """Plain values only. A nested value is dropped rather than copied."""
    found: dict[str, ExtraValue] = {}
    for key, value in (as_dict(raw) or {}).items():
        if not key or len(key) > MAX_CUSTOM_KEY_LENGTH or len(found) >= MAX_EXTRAS_KEYS:
            continue
        if isinstance(value, str | bool):
            found[key] = value
        elif (number := number_of(value)) is not None:
            found[key] = number
    return found


def map_details(listing_id: str, raw: object) -> ListingDetails:
    item = as_dict(raw)
    if item is None:
        raise PMSUpstreamError("Guesty returned a listing that could not be understood.")
    try:
        return ListingDetails(
            listing_id=listing_id,
            descriptions=_descriptions(item),
            amenities=strings_of(item.get("amenities"), limit=MAX_AMENITIES),
            amenities_not_included=strings_of(
                item.get("amenitiesNotIncluded"), limit=MAX_AMENITIES
            ),
            house_rules=None,
            rooms=[],
            check_in_instructions=_check_in_instructions(item.get("checkInInstructions")),
            custom_fields=_custom_fields(item.get("customFields")),
        )
    except ValidationError:
        logger.warning("listing details could not be mapped", extra={"listing_id": listing_id[:80]})
        raise PMSUpstreamError(
            "Guesty returned listing details that could not be understood."
        ) from None


def map_photo(raw: object, fallback_position: int) -> tuple[int, ListingPhoto] | None:
    """A photo and its sort key, or None (logged by id only) when it cannot be mapped."""
    item = as_dict(raw)
    photo_id = text_of(item.get("_id")) if item is not None else None
    url = text_of(item.get("original")) if item is not None else None
    if item is None or photo_id is None or url is None or not _URL_PATTERN.match(url):
        logger.warning("photo skipped: no usable id or address")
        return None
    thumbnail = text_of(item.get("thumbnail"))
    index = count_of(item.get("index"))
    position = index if index is not None else fallback_position
    try:
        photo = ListingPhoto(
            id=photo_id,
            url=url,
            thumbnail_url=thumbnail if thumbnail and _URL_PATTERN.match(thumbnail) else None,
            caption=text_of(item.get("caption")),
            position=position,
        )
    except ValidationError:
        logger.warning("photo skipped: could not be mapped", extra={"photo_id": photo_id[:80]})
        return None
    return position, photo


class GuestyContent:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def get_listing_details(self, listing_id: str) -> ListingDetails:
        response = await self._client.request(
            "GET", f"/listings/{quote_id(listing_id)}", params={"fields": DETAIL_FIELDS}
        )
        return map_details(listing_id.strip(), response.json())

    async def list_listing_photos(self, listing_id: str) -> list[ListingPhoto]:
        path = f"/properties-api/property-photos/property-photos/{quote_id(listing_id)}"
        response = await self._client.request("GET", path)
        raw = as_list(response.json())
        if raw is None:
            raise PMSUpstreamError("Guesty returned a photo list that could not be understood.")
        mapped = [
            photo
            for index, entry in enumerate(raw[:MAX_PHOTOS])
            if (photo := map_photo(entry, index))
        ]
        # Sorted by Guesty's position (a stable sort keeps the order given for equal positions).
        return [photo for _, photo in sorted(mapped, key=lambda pair: pair[0])]

    async def update_photo_caption(self, update: PhotoCaptionUpdate) -> ListingPhoto:
        raise CapabilityNotSupportedError(Provider.GUESTY, Capability.PHOTOS_WRITE)

    async def reorder_photos(self, order: PhotoOrder) -> list[ListingPhoto]:
        raise CapabilityNotSupportedError(Provider.GUESTY, Capability.PHOTOS_WRITE)

    async def delete_photo(self, photo: PhotoDelete) -> None:
        raise CapabilityNotSupportedError(Provider.GUESTY, Capability.PHOTOS_WRITE)


def build(client: GuestyClient) -> GuestyContent:
    return GuestyContent(client)
