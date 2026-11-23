"""Guesty reviews: list the reviews and answer one in public.

Endpoints (see docs/providers/guesty/reviews.md): `GET /reviews` and `PUT /reviews/{id}/reply`.
Review text is written by guests and is untrusted: it is only ever passed on inside the canonical
`Review` and is never logged, and neither is the reply text. The name of the guest who wrote a
review is never read.

Replying is one PUT and is never sent again (a 429 with Retry-After is the only exception, handled
by the shared client). Guesty has no call to read one review, so before replying this adapter looks
for the review in the newest reviews and refuses, sending nothing, when it already has a reply or
cannot be found. Shared helpers are in `common.py`.
"""

import logging
from collections.abc import Mapping
from datetime import date
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import (
    Capability,
    PMSNotFoundError,
    PMSUpstreamError,
    PMSValidationError,
    remaining_seconds,
)
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    MAX_SKIP,
    as_dict,
    as_list,
    count_of,
    datetime_of,
    decode_cursor,
    encode_cursor,
    number_of,
    quote_id,
    text_of,
)
from pmsbridge.pms.models import (
    MAX_PAGE_SIZE,
    ExtraValue,
    Page,
    Review,
    ReviewQuery,
    ReviewReply,
)

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.REVIEWS_READ, Capability.REVIEWS_REPLY})

REVIEWS_PATH: Final = "/reviews"
MIN_SECONDS_PER_WRITE: Final = 2.0
# Guesty documents no way to read one review and no way to filter by its id, so the review to
# answer is looked for in the newest ones: at most this many pages of MAX_PAGE_SIZE.
MAX_SCAN_PAGES: Final = 10
# Documented for the one review shape whose rating is described: 1 to 10.
RATING_SCALE: Final = 10.0
# Airbnb reviews (`channelId` airbnb2) keep the review in `rawReview`, rated 1 to 5 (seen live).
AIRBNB_RATING_SCALE: Final = 5.0
# A reply in one of these states was not made, so a new one may be tried.
_NO_REPLY_STATES: Final = frozenset({"FAILED", "NOT_FOUND"})


# ---- Mapping ----


def _scalars(values: Mapping[str, ExtraValue]) -> dict[str, ExtraValue]:
    return {key: value for key, value in values.items() if value is not None}


def _active_replies(item: Mapping[str, object]) -> list[dict[str, object]]:
    """The replies that exist or are on their way (not failed, not missing), newest as given."""
    found: list[dict[str, object]] = []
    for entry in as_list(item.get("reviewReplies")) or []:
        reply = as_dict(entry)
        if reply is None:
            continue
        status = (text_of(reply.get("status")) or "").upper()
        if status not in _NO_REPLY_STATES:
            found.append(reply)
    return found


def _item_of(raw: object) -> dict[str, object] | None:
    """The review object itself: some answers wrap it in `data`."""
    item = as_dict(raw)
    if item is None:
        return None
    if "_id" not in item and (inner := as_dict(item.get("data"))) is not None:
        return inner
    return item


def map_review(raw: object) -> Review | None:
    """The canonical review, or None (logged without any detail) for an unusable item."""
    item = _item_of(raw)
    review_id = text_of(item.get("_id")) if item is not None else None
    if item is None or review_id is None:
        logger.warning("review skipped: no usable id")
        return None
    contents = as_dict(item.get("contents")) or {}
    airbnb = as_dict(item.get("rawReview")) or {}
    rating, scale = number_of(contents.get("rating")), RATING_SCALE
    if rating is None and (overall := number_of(airbnb.get("overall_rating"))) is not None:
        rating, scale = overall, AIRBNB_RATING_SCALE
    if rating is not None and rating < 0:
        rating = None
    body = contents.get("body")
    if not isinstance(body, str):
        body = airbnb.get("public_review")
    private = airbnb.get("private_feedback")
    replies = _active_replies(item)
    reply_texts = [text for entry in replies if (text := text_of(entry.get("reviewReply")))]
    if not reply_texts and (answered := text_of(airbnb.get("reviewee_response"))) is not None:
        reply_texts = [answered]
    first = replies[0] if replies else {}
    extras = _scalars(
        {
            "reply_status": text_of(first.get("status")),
            "replied_at": text_of(first.get("replyAt")),
            "channel_review_id": text_of(item.get("externalReviewId")),
            "updated_at": text_of(item.get("updatedAtGuesty")) or text_of(item.get("updatedAt")),
        }
    )
    try:
        return Review(
            id=review_id,
            listing_id=text_of(item.get("listingId")),
            reservation_id=text_of(item.get("reservationId")),
            channel=text_of(item.get("channelId")),
            rating=None if rating is None else float(rating),
            rating_scale=None if rating is None else scale,
            public_text=text_of(body) if isinstance(body, str) else None,
            # Airbnb's private feedback is written to the host only, never shown in public.
            private_text=text_of(private) if isinstance(private, str) else None,
            reply=reply_texts[0] if reply_texts else None,
            created_at=(
                datetime_of(item.get("createdAt"), naive_as_utc=True)
                or datetime_of(item.get("createdAtGuesty"), naive_as_utc=True)
            ),
            extras=extras,
        )
    except ValidationError:
        # The error text would repeat the guest's words, so only the id is logged.
        logger.warning("review skipped: could not be mapped", extra={"review_id": review_id[:80]})
        return None


def _page_of(body: object) -> tuple[list[object], int | None]:
    """The items of a list answer, and the page size Guesty says it used.

    Guesty's reference types `data` only as an object, so a bare array, `data` as an array and
    `data.results` are all accepted.
    """
    found = as_list(body)
    envelope = as_dict(body)
    if found is None and envelope is not None:
        data = envelope.get("data")
        found = as_list(data)
        if found is None and (inner := as_dict(data)) is not None:
            found = as_list(inner.get("results"))
        if found is None:
            found = as_list(envelope.get("results"))
    if found is None:
        raise PMSUpstreamError("Guesty returned a review list that could not be understood.")
    if len(found) > MAX_PAGE_SIZE:
        raise PMSUpstreamError("Guesty returned more reviews than expected.")
    size = count_of(envelope.get("limit")) if envelope is not None else None
    return found, size or None


def _start_of_day(day: date) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"


# ---- Adapter ----


class GuestyReviews:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_reviews(self, query: ReviewQuery) -> Page[Review]:
        skip = decode_cursor(query.cursor)
        raw, size = await self._page(query.listing_id, query.since, query.limit, skip)
        raw = raw[: query.limit]
        items = [found for entry in raw if (found := map_review(entry)) is not None]
        # Guesty may use a smaller page than asked for, so a page that fills Guesty's own size
        # still counts as a full one.
        full = min(query.limit, size) if size else query.limit
        consumed = skip + len(raw)
        more = bool(raw) and len(raw) >= full and consumed <= MAX_SKIP
        return Page[Review](items=items, next_cursor=encode_cursor(consumed) if more else None)

    async def reply_to_review(self, reply: ReviewReply) -> Review:
        wanted = reply.review_id.strip()
        quote_id(wanted)
        found = await self._find(wanted)
        airbnb = as_dict(found.get("rawReview")) or {}
        if _active_replies(found) or text_of(airbnb.get("reviewee_response")) is not None:
            raise PMSValidationError(
                "This review already has a reply, so nothing was sent. Edit or remove the reply "
                "in Guesty or on the booking channel."
            )
        left = remaining_seconds()
        if left is not None and left < MIN_SECONDS_PER_WRITE:
            raise PMSUpstreamError("Nothing was sent: the time allowed ran out. Try again.")
        response = await self._client.request(
            "PUT",
            f"{REVIEWS_PATH}/{quote_id(wanted)}/reply",
            json_body={"reviewReply": reply.body},
        )
        unclear = PMSUpstreamError(
            "Guesty accepted the reply but its answer could not be understood. The reply was "
            "probably sent: check the review in Guesty before trying again."
        )
        try:
            body = response.json()
        except PMSUpstreamError:
            raise unclear from None
        sent = map_review(body)
        if sent is None:
            raise unclear
        return sent

    async def _page(
        self, listing_id: str | None, since: date | None, limit: int, skip: int
    ) -> tuple[list[object], int | None]:
        # Custom channel reviews are asked for because they are the only reviews whose rating and
        # text Guesty's reference describes.
        params: dict[str, str | int] = {
            "includeCustomChannels": "true",
            "limit": limit,
            "skip": skip,
        }
        if listing_id is not None:
            params["listingId"] = listing_id
        if since is not None:
            params["startDate"] = _start_of_day(since)
        response = await self._client.request("GET", REVIEWS_PATH, params=params)
        return _page_of(response.json())

    async def _find(self, review_id: str) -> dict[str, object]:
        """The review in the newest reviews, or an error that sends nothing."""
        skip = 0
        for _ in range(MAX_SCAN_PAGES):
            raw, size = await self._page(None, None, MAX_PAGE_SIZE, skip)
            for entry in raw:
                item = _item_of(entry)
                if item is not None and text_of(item.get("_id")) == review_id:
                    return item
            skip += len(raw)
            if not raw or len(raw) < min(MAX_PAGE_SIZE, size or MAX_PAGE_SIZE) or skip > MAX_SKIP:
                break
        raise PMSNotFoundError(
            "That review was not found among the most recently updated reviews, so nothing was "
            "sent."
        )


def build(client: GuestyClient) -> GuestyReviews:
    return GuestyReviews(client)
