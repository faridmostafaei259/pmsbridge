"""Guesty inbox: list conversations, read a conversation's messages, send a message to the guest.

Endpoints (see docs/providers/guesty/inbox.md), all under `/communication/conversations`:
list, `{id}/posts` (the messages) and `{id}/send-message`. Message text is written by guests and by
other people: it is untrusted, is only ever passed on inside the canonical `Message`, and is never
logged. Guest email addresses and phone numbers in Guesty's answers are never read.

Sending is one POST and is never sent again (a 429 with Retry-After is the only exception, handled
by the shared client). Guesty does not say which channel a conversation uses or allows, so before
sending this adapter reads the newest messages and only uses a channel that conversation has
already used. Guesty documents no post that stays hidden from the guest, so adding a note is
refused with a clear error (`INBOX_NOTE` is not declared). Shared helpers are in `common.py`.
"""

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from pydantic import ValidationError

from pmsbridge.pms.base import (
    Capability,
    CapabilityNotSupportedError,
    PMSUpstreamError,
    PMSValidationError,
    Provider,
    remaining_seconds,
)
from pmsbridge.pms.guesty.client import GuestyClient
from pmsbridge.pms.guesty.common import (
    as_dict,
    as_list,
    datetime_of,
    decode_token_cursor,
    encode_token_cursor,
    flag_of,
    number_of,
    quote_id,
    text_of,
)
from pmsbridge.pms.models import (
    MAX_PAGE_SIZE,
    Conversation,
    ConversationNote,
    ConversationQuery,
    ExtraValue,
    Message,
    MessageChannel,
    MessageDirection,
    OutgoingMessage,
    Page,
    PageRequest,
)

logger = logging.getLogger(__name__)

CAPABILITIES: Final = frozenset({Capability.INBOX_READ, Capability.INBOX_SEND})

CONVERSATIONS_PATH: Final = "/communication/conversations"
# Guesty's default fields leave out the channel (`integration`) and the last message (`state`).
# Conversations are sorted by their latest activity (`modifiedAt`, which moves with each message);
# Guesty only returns a paging cursor when the sort field is in the list, so it must stay there.
CONVERSATION_FIELDS: Final = (
    "guest reservation status assignee priority createdAt modifiedAt integration state"
)
CONVERSATION_SORT: Final = "-modifiedAt"
MIN_SECONDS_PER_WRITE: Final = 2.0

# `module.type` values that reach the guest. `note` and `log` never do. `airbnb2` has no canonical
# channel, so it is only used to answer on the conversation's own channel.
GUEST_CHANNELS: Final = frozenset({"platform", "airbnb2", "email", "sms", "whatsapp"})
_REQUESTED_CHANNEL: Final = {
    MessageChannel.PLATFORM: "platform",
    MessageChannel.EMAIL: "email",
    MessageChannel.SMS: "sms",
    MessageChannel.WHATSAPP: "whatsapp",
}
NOTE_REFUSED: Final = (
    "Adding a note is not available: Guesty documents no way to add a message that stays hidden "
    "from the guest, so nothing was sent."
)


# ---- Mapping ----


def _latest(*values: object) -> datetime | None:
    found = [moment for value in values if (moment := datetime_of(value, naive_as_utc=True))]
    return max(found) if found else None


def _scalars(values: Mapping[str, ExtraValue]) -> dict[str, ExtraValue]:
    return {key: value for key, value in values.items() if value is not None}


def map_conversation(raw: object) -> Conversation | None:
    """The canonical conversation, or None (logged without any detail) for an unusable item."""
    item = as_dict(raw)
    conversation_id = text_of(item.get("_id")) if item is not None else None
    if item is None or conversation_id is None:
        logger.warning("conversation skipped: no usable id")
        return None
    state = as_dict(item.get("state")) or {}
    meta = as_dict(item.get("meta")) or {}
    guest = as_dict(meta.get("guest")) or {}
    integration = as_dict(meta.get("integration")) or as_dict(item.get("integration")) or {}
    channel = text_of(integration.get("platform"))
    reservations = [d for entry in as_list(meta.get("reservations")) or [] if (d := as_dict(entry))]
    first = reservations[0] if reservations else {}
    listing = as_dict(first.get("listing")) or {}
    last = as_dict(state.get("lastMessage")) or {}
    last_from = as_dict(item.get("lastMessageFrom")) or {}
    read = flag_of(state.get("read"))
    assignee = as_dict(item.get("assignee")) or {}
    extras = _scalars(
        {
            "status": text_of(state.get("status")),
            "reservation_status": text_of(first.get("status")),
            "confirmation_code": text_of(first.get("confirmationCode")),
            "check_in": text_of(first.get("checkIn")),
            "check_out": text_of(first.get("checkOut")),
            "listing_title": text_of(listing.get("title")),
            "reservation_count": len(reservations) if len(reservations) > 1 else None,
            "returning_guest": flag_of(guest.get("isReturning")),
            "assignee_id": text_of(assignee.get("_id")),
            "priority": number_of(item.get("priority")),
        }
    )
    try:
        return Conversation(
            id=conversation_id,
            reservation_id=text_of(first.get("_id")),
            listing_id=text_of(listing.get("_id")),
            guest_id=text_of(guest.get("_id")),
            guest_name=text_of(guest.get("fullName")),
            channel=channel.lower() if channel is not None else None,
            last_message_at=_latest(last.get("date"))
            or _latest(last_from.get("user"), last_from.get("nonUser")),
            unread=None if read is None else not read,
            extras=extras,
        )
    except ValidationError:
        # The error text would repeat the guest's values, so only the id is logged.
        logger.warning(
            "conversation skipped: could not be mapped",
            extra={"conversation_id": conversation_id[:80]},
        )
        return None


def _direction(post: Mapping[str, object], channel: str | None) -> MessageDirection:
    """Who wrote a post. Guesty has no direction field, so it is read from `sentBy` and `from`.

    When nothing says who wrote it, the post counts as inbound: text from the guest is the one
    kind that is treated as untrusted, so that is the safe label.
    """
    sent_by = (text_of(post.get("sentBy")) or "").lower()
    author = as_dict(post.get("from")) or {}
    author_type = (text_of(author.get("type")) or "").lower()
    if channel == "log" or sent_by == "log":
        return MessageDirection.SYSTEM
    if sent_by == "guest":
        return MessageDirection.INBOUND
    if sent_by == "host" or channel == "note" or author_type == "user":
        return MessageDirection.OUTBOUND
    return MessageDirection.INBOUND


def map_post(raw: object, conversation_id: str) -> Message | None:
    """The canonical message, or None (logged without any detail) for an unusable item."""
    post = as_dict(raw)
    post_id = text_of(post.get("_id")) if post is not None else None
    if post is None or post_id is None:
        logger.warning("message skipped: no usable id")
        return None
    module = as_dict(post.get("module")) or {}
    channel = text_of(module.get("type"))
    channel = channel.lower() if channel is not None else None
    direction = _direction(post, channel)
    author = as_dict(post.get("from")) or {}
    body = post.get("body")
    try:
        return Message(
            id=post_id,
            conversation_id=conversation_id,
            direction=direction,
            body=body if isinstance(body, str) else "",
            sent_at=datetime_of(post.get("createdAt"), naive_as_utc=True),
            channel=channel,
            # A guest's own name is never added: only staff and system authors are named.
            author_name=(
                text_of(author.get("fullName"))
                if direction is not MessageDirection.INBOUND
                else None
            ),
        )
    except ValidationError:
        logger.warning("message skipped: could not be mapped", extra={"message_id": post_id[:80]})
        return None


def _data(body: object, what: str) -> dict[str, object]:
    envelope = as_dict(body)
    data = as_dict(envelope.get("data")) if envelope is not None else None
    if data is None:
        raise PMSUpstreamError(f"Guesty returned {what} that could not be understood.")
    return data


def _next_cursor(data: Mapping[str, object], count: int) -> str | None:
    """The cursor for the next page. Guesty's empty cursor, or an empty page, means the end."""
    if count == 0:
        return None
    after = as_dict(data.get("cursor")) or {}
    token = text_of(after.get("after"))
    if token is None:
        return None
    cursor = encode_token_cursor(token)
    if cursor is None:
        logger.warning("paging stopped: Guesty gave a cursor that cannot be used")
    return cursor


def _items(data: Mapping[str, object], key: str, what: str) -> list[object]:
    items = as_list(data.get(key))
    if items is None:
        raise PMSUpstreamError(f"Guesty returned {what} that could not be understood.")
    if len(items) > MAX_PAGE_SIZE:
        raise PMSUpstreamError(f"Guesty returned more {what} than expected.")
    return items


def _filter(field: str, value: object) -> dict[str, object]:
    return {"field": field, "operator": "$eq", "value": value}


# ---- Adapter ----


class GuestyInbox:
    def __init__(self, client: GuestyClient) -> None:
        self._client = client

    async def list_conversations(self, query: ConversationQuery) -> Page[Conversation]:
        filters: list[dict[str, object]] = []
        if query.reservation_id is not None:
            filters.append(_filter("reservation._id", _plain_id(query.reservation_id)))
        if query.listing_id is not None:
            filters.append(_filter("listing._id", _plain_id(query.listing_id)))
        if query.unread_only:
            filters.append(_filter("read", False))
        params: dict[str, str | int] = {
            "type": "guest",
            "limit": query.limit,
            "sort": CONVERSATION_SORT,
            "fields": CONVERSATION_FIELDS,
        }
        if filters:
            params["filters"] = json.dumps(filters, separators=(",", ":"))
        after = decode_token_cursor(query.cursor)
        if after is not None:
            params["cursorAfter"] = after
        response = await self._client.request("GET", CONVERSATIONS_PATH, params=params)
        data = _data(response.json(), "a conversation list")
        raw = _items(data, "conversations", "conversations")
        items = [found for entry in raw if (found := map_conversation(entry)) is not None]
        return Page[Conversation](items=items, next_cursor=_next_cursor(data, len(raw)))

    async def get_conversation_messages(
        self, conversation_id: str, page: PageRequest
    ) -> Page[Message]:
        # Guesty documents no page size for messages, so `page.limit` cannot be applied: the
        # page is whatever Guesty returns (25 by default), newest first.
        wanted = _plain_id(conversation_id)
        after = decode_token_cursor(page.cursor)
        raw, data = await self._posts(wanted, after)
        items = [found for entry in raw if (found := map_post(entry, wanted)) is not None]
        return Page[Message](items=items, next_cursor=_next_cursor(data, len(raw)))

    async def send_guest_message(self, message: OutgoingMessage) -> Message:
        wanted = _plain_id(message.conversation_id)
        raw, _ = await self._posts(wanted, None)
        module_type = _module_type(message.channel, _channels_used(raw))
        left = remaining_seconds()
        if left is not None and left < MIN_SECONDS_PER_WRITE:
            raise PMSUpstreamError("Nothing was sent: the time allowed ran out. Try again.")
        response = await self._client.request(
            "POST",
            f"{CONVERSATIONS_PATH}/{quote_id(wanted)}/send-message",
            json_body={"module": {"type": module_type}, "body": message.body},
        )
        unclear = PMSUpstreamError(
            "Guesty accepted the message but its answer could not be understood. The message "
            "was probably sent: check the conversation in Guesty before trying again."
        )
        try:
            body = response.json()
        except PMSUpstreamError:
            raise unclear from None
        sent = map_post(_sent_post(body), wanted)
        if sent is None:
            raise unclear
        return sent.model_copy(update={"direction": MessageDirection.OUTBOUND})

    async def add_conversation_note(self, note: ConversationNote) -> Message:
        raise CapabilityNotSupportedError(Provider.GUESTY, Capability.INBOX_NOTE)

    async def _posts(
        self, conversation_id: str, after: str | None
    ) -> tuple[list[object], dict[str, object]]:
        params: dict[str, str | int] = {"sort": "-createdAt"}
        if after is not None:
            params["cursorAfter"] = after
        response = await self._client.request(
            "GET", f"{CONVERSATIONS_PATH}/{quote_id(conversation_id)}/posts", params=params
        )
        data = _data(response.json(), "a message list")
        return _items(data, "posts", "messages"), data


def _plain_id(value: str) -> str:
    """A checked id (the same rules as `quote_id`), not yet quoted."""
    quote_id(value)
    return value.strip()


def _channels_used(raw: list[object]) -> list[str]:
    """The guest-facing channels the newest messages used, newest first, without repeats."""
    found: list[str] = []
    for entry in raw:
        post = as_dict(entry) or {}
        module = as_dict(post.get("module")) or {}
        kind = (text_of(module.get("type")) or "").lower()
        if kind in GUEST_CHANNELS and kind not in found:
            found.append(kind)
    return found


def _module_type(requested: MessageChannel, used: list[str]) -> str:
    if requested is MessageChannel.DEFAULT:
        if not used:
            raise PMSValidationError(
                "The channel of this conversation cannot be told because it has no earlier "
                "message, so nothing was sent. Reply from Guesty directly."
            )
        return used[0]
    wanted = _REQUESTED_CHANNEL[requested]
    if wanted not in used:
        used_text = ", ".join(used) if used else "none yet"
        raise PMSValidationError(
            f"The channel '{requested.value}' is not available for this conversation, so nothing "
            f"was sent. Channels it has used: {used_text}."
        )
    return wanted


def _sent_post(body: object) -> object:
    """The post in a send answer: Guesty's reference shows both `{data: {post}}` and `{data}`."""
    envelope = as_dict(body)
    data = as_dict(envelope.get("data")) if envelope is not None else None
    if data is None:
        return None
    return data.get("post") if "post" in data else data


def build(client: GuestyClient) -> GuestyInbox:
    return GuestyInbox(client)
