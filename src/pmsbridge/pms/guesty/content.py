"""Guesty listing content: full details and photos, and three photo changes.

Endpoints (see docs/providers/guesty/listings.md): `GET /listings/{id}` for details and, under
`/properties-api/property-photos/property-photos/{id}`, the photo list (GET), a caption change
(`POST .../{photoId}` with the caption only, never an address), a new order (`POST .../order`) and a
deletion (`DELETE .../{photoId}`).

Every photo change reads the photo list first, so an id that is not in it is refused before anything
is sent, and is then one request that is never sent again (a 429 with Retry-After is the only
exception, handled by the shared client). A timeout means the outcome is unknown. A deletion is
confirmed by reading the list again, because Guesty's answer to it is not documented and is never
relied on. Deleting cannot be undone.

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
    PMSError,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
    remaining_seconds,
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
from pmsbridge.pms.http import UpstreamResponse
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

CAPABILITIES: Final = frozenset(
    {Capability.LISTING_DETAILS_READ, Capability.PHOTOS_READ, Capability.PHOTOS_WRITE}
)

# `privateDescription` (staff-only text) is deliberately never requested.
DETAIL_FIELDS: Final = (
    "_id publicDescription amenities amenitiesNotIncluded checkInInstructions customFields"
)
MAX_AMENITIES: Final = 200
MAX_DESCRIPTIONS: Final = 20
MAX_CUSTOM_KEY_LENGTH: Final = 100
PHOTOS_PATH: Final = "/properties-api/property-photos/property-photos"
MIN_SECONDS_PER_WRITE: Final = 2.0
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
        response = await self._client.request("GET", f"{PHOTOS_PATH}/{quote_id(listing_id)}")
        return _photos(response.json())

    async def update_photo_caption(self, update: PhotoCaptionUpdate) -> ListingPhoto:
        listing_id, photo_id = update.listing_id.strip(), update.photo_id.strip()
        await self._require_photos(listing_id, [photo_id])
        _require_time("changed")
        # Only the caption is sent. An address would replace the image, so it is never sent.
        response = await self._client.request(
            "POST",
            f"{PHOTOS_PATH}/{quote_id(listing_id)}/{quote_id(photo_id)}",
            json_body={"caption": update.caption},
        )
        photos = _answer_photos(response, "changed")
        changed = next((photo for photo in photos if photo.id == photo_id), None)
        if changed is None:
            raise _unclear("changed")
        return changed

    async def reorder_photos(self, order: PhotoOrder) -> list[ListingPhoto]:
        listing_id = order.listing_id.strip()
        current = await self._require_photos(listing_id, order.photo_ids)
        missing = len({photo.id for photo in current} - set(order.photo_ids))
        if missing:
            raise PMSValidationError(
                f"The order leaves out {missing} of the listing's photos, so nothing was changed. "
                "Give every photo id once, as listed by list_listing_photos."
            )
        _require_time("reordered")
        response = await self._client.request(
            "POST",
            f"{PHOTOS_PATH}/{quote_id(listing_id)}/order",
            json_body={"order": list(order.photo_ids)},
        )
        return _answer_photos(response, "reordered")

    async def delete_photo(self, photo: PhotoDelete) -> None:
        listing_id, photo_id = photo.listing_id.strip(), photo.photo_id.strip()
        await self._require_photos(listing_id, [photo_id])
        _require_time("deleted")
        # Guesty's answer to a deletion is not documented, so only the status is used.
        await self._client.request(
            "DELETE", f"{PHOTOS_PATH}/{quote_id(listing_id)}/{quote_id(photo_id)}"
        )
        try:
            remaining = await self.list_listing_photos(listing_id)
        except PMSError as exc:
            logger.warning(
                "photo deletion could not be confirmed",
                extra={"listing_id": listing_id[:80], "error_class": type(exc).__name__},
            )
            raise PMSUpstreamError(
                "Guesty accepted the deletion, but the photo list could not be read afterwards "
                "to confirm it. Check with list_listing_photos before trying again."
            ) from None
        if any(found.id == photo_id for found in remaining):
            raise PMSUpstreamError(
                "Guesty accepted the deletion, but the photo is still in the list. Check with "
                "list_listing_photos before trying again."
            )

    async def _require_photos(self, listing_id: str, photo_ids: list[str]) -> list[ListingPhoto]:
        """The listing's photos, after checking that every wanted id is one of them."""
        current = await self.list_listing_photos(listing_id)
        known = {photo.id for photo in current}
        if any(photo_id not in known for photo_id in photo_ids):
            raise PMSNotFoundError(
                "A photo id is not one of this listing's photos, so nothing was changed. "
                "Use the ids from list_listing_photos."
            )
        return current


def _photos(body: object) -> list[ListingPhoto]:
    """The photos of a list or change answer (a bare array), in Guesty's order of position."""
    raw = as_list(body)
    if raw is None:
        raise PMSUpstreamError("Guesty returned a photo list that could not be understood.")
    mapped = [
        photo for index, entry in enumerate(raw[:MAX_PHOTOS]) if (photo := map_photo(entry, index))
    ]
    # Sorted by Guesty's position (a stable sort keeps the order given for equal positions).
    return [photo for _, photo in sorted(mapped, key=lambda pair: pair[0])]


def _unclear(done: str) -> PMSUpstreamError:
    return PMSUpstreamError(
        f"Guesty accepted the request but its answer could not be understood. The photos were "
        f"probably {done}: check with list_listing_photos before trying again."
    )


def _answer_photos(response: UpstreamResponse, done: str) -> list[ListingPhoto]:
    try:
        return _photos(response.json())
    except PMSUpstreamError:
        raise _unclear(done) from None


def _require_time(done: str) -> None:
    left = remaining_seconds()
    if left is not None and left < MIN_SECONDS_PER_WRITE:
        raise PMSUpstreamError(f"Nothing was {done}: the time allowed ran out. Try again.")


def build(client: GuestyClient) -> GuestyContent:
    return GuestyContent(client)
