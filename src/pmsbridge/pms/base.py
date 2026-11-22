"""Provider contract shared by every PMS adapter.

A ``PMSProvider`` instance is bound to exactly one PMS connection (credentials and token handling
are injected by the registry when the instance is created). Its domain APIs therefore take no
connection or credential arguments.

A provider exposes one attribute per domain. An attribute is ``None`` when the provider does not
support that domain at all; finer-grained support (for example read but not write) is expressed
through ``capabilities``. Callers use ``require_api`` before calling a method, so that an
unsupported feature always produces a clear ``CapabilityNotSupportedError``.

Rules every adapter must follow:

- Retries: read operations may be retried on timeouts, 5xx and 429. Write operations are not
  idempotent and are NEVER retried automatically, except after a 429 response that carried
  ``Retry-After`` (the PMS did not process the request). If a write times out or the connection
  drops, raise ``PMSUpstreamError`` with a message saying the outcome is unknown and should be
  checked before trying again.
- Deadlines: the MCP layer sets ``call_deadline`` for each tool call. Adapters must not wait (for
  rate limits, ``Retry-After`` or backoff) beyond ``remaining_seconds()``; if the wait would exceed
  it, raise ``PMSRateLimitedError`` with the retry delay instead of waiting.
- Notes: ``add_reservation_note`` appends to existing notes and never overwrites them. Providers
  that store notes as one field must re-read the reservation immediately before writing.
- Errors: every ``PMSError`` message must be safe to show to an AI client (no secrets, tokens,
  stack traces or raw PMS responses).
- Base URLs are fixed in code. No user-supplied value may change the host an adapter calls.

Adding a provider requires a new ``Provider`` member here (a reviewed contract change) plus the
adapter package; MCP tools do not change.

Changing this module is a contract change and is reserved for the maintainers of the core.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pmsbridge.pms.models import (
    CalendarDay,
    CalendarRange,
    CalendarUpdateRequest,
    Conversation,
    ConversationQuery,
    Guest,
    GuestQuery,
    Listing,
    ListingQuery,
    Message,
    NewTask,
    OutgoingMessage,
    Owner,
    Page,
    PageRequest,
    Payment,
    Reservation,
    ReservationNote,
    ReservationQuery,
    Review,
    ReviewQuery,
    ReviewReply,
    Task,
    TaskQuery,
)


class Provider(StrEnum):
    GUESTY = "guesty"
    HOSTAWAY = "hostaway"


class Capability(StrEnum):
    LISTINGS_READ = "listings.read"
    RESERVATIONS_READ = "reservations.read"
    RESERVATIONS_ADD_NOTE = "reservations.add_note"
    CALENDAR_READ = "calendar.read"
    CALENDAR_WRITE = "calendar.write"
    GUESTS_READ = "guests.read"
    INBOX_READ = "inbox.read"
    INBOX_SEND = "inbox.send"
    REVIEWS_READ = "reviews.read"
    REVIEWS_REPLY = "reviews.reply"
    OWNERS_READ = "owners.read"
    PAYMENTS_READ = "payments.read"
    TASKS_READ = "tasks.read"
    TASKS_CREATE = "tasks.create"


WRITE_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.RESERVATIONS_ADD_NOTE,
        Capability.CALENDAR_WRITE,
        Capability.INBOX_SEND,
        Capability.REVIEWS_REPLY,
        Capability.TASKS_CREATE,
    }
)


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    """Non-secret description of a PMS connection, safe to log and to show to tools."""

    connection_id: str
    provider: Provider
    label: str


# --------------------------------------------------------------------------- deadlines

call_deadline: ContextVar[float | None] = ContextVar("call_deadline", default=None)
"""Monotonic-clock deadline (``time.monotonic()``) for the current tool call, or None."""


def remaining_seconds() -> float | None:
    """Seconds left before the current call's deadline, or None when no deadline is set."""
    deadline = call_deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


# --------------------------------------------------------------------------- errors


class PMSError(Exception):
    """Base class for all provider errors. Messages must be safe to show to an AI client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CapabilityNotSupportedError(PMSError):
    def __init__(self, provider: Provider, capability: Capability) -> None:
        super().__init__(f"{provider.value} does not support '{capability.value}'.")
        self.provider = provider
        self.capability = capability


class PMSAuthError(PMSError):
    """Credentials rejected or token unusable. The connection needs attention from an admin."""


class PMSPermissionError(PMSError):
    """The PMS account is authenticated but not allowed to perform this operation."""


class PMSNotFoundError(PMSError):
    pass


class PMSRateLimitedError(PMSError):
    def __init__(self, message: str, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PMSTokenBudgetError(PMSError):
    """The provider's token issuance limit would be exceeded; no new token may be requested now."""


class PMSValidationError(PMSError):
    """The request (including a pagination cursor) is invalid or was rejected by the PMS."""


class PMSUpstreamError(PMSError):
    """The PMS failed (5xx, timeout, malformed response) or a write's outcome is unknown."""


# --------------------------------------------------------------------------- domain APIs


class ListingsAPI(Protocol):
    async def list_listings(self, query: ListingQuery) -> Page[Listing]: ...
    async def get_listing(self, listing_id: str) -> Listing: ...


class ReservationsAPI(Protocol):
    async def list_reservations(self, query: ReservationQuery) -> Page[Reservation]: ...
    async def get_reservation(self, reservation_id: str) -> Reservation: ...
    async def add_reservation_note(self, note: ReservationNote) -> Reservation:
        """Append ``note`` to the reservation's notes. Never overwrite existing notes."""
        ...


class CalendarAPI(Protocol):
    async def get_calendar(self, calendar_range: CalendarRange) -> list[CalendarDay]: ...
    async def update_calendar(self, request: CalendarUpdateRequest) -> list[CalendarDay]: ...


class GuestsAPI(Protocol):
    async def list_guests(self, query: GuestQuery) -> Page[Guest]: ...
    async def get_guest(self, guest_id: str) -> Guest: ...


class InboxAPI(Protocol):
    async def list_conversations(self, query: ConversationQuery) -> Page[Conversation]: ...
    async def get_conversation_messages(
        self, conversation_id: str, page: PageRequest
    ) -> Page[Message]: ...
    async def send_guest_message(self, message: OutgoingMessage) -> Message:
        """Send a message. ``MessageChannel.DEFAULT`` replies on the conversation's own channel.

        Raise ``PMSValidationError`` if the requested channel is not available for the conversation.
        """
        ...


class ReviewsAPI(Protocol):
    async def list_reviews(self, query: ReviewQuery) -> Page[Review]: ...
    async def reply_to_review(self, reply: ReviewReply) -> Review: ...


class OwnersAPI(Protocol):
    async def list_owners(self, page: PageRequest) -> Page[Owner]: ...
    async def get_owner(self, owner_id: str) -> Owner: ...


class PaymentsAPI(Protocol):
    async def list_reservation_payments(self, reservation_id: str) -> list[Payment]: ...


class TasksAPI(Protocol):
    async def list_tasks(self, query: TaskQuery) -> Page[Task]: ...
    async def create_task(self, task: NewTask) -> Task: ...


@runtime_checkable
class PMSProvider(Protocol):
    """One provider instance per connection."""

    @property
    def provider(self) -> Provider: ...

    @property
    def connection(self) -> ConnectionRef: ...

    @property
    def capabilities(self) -> frozenset[Capability]: ...

    @property
    def listings(self) -> ListingsAPI | None: ...

    @property
    def reservations(self) -> ReservationsAPI | None: ...

    @property
    def calendar(self) -> CalendarAPI | None: ...

    @property
    def guests(self) -> GuestsAPI | None: ...

    @property
    def inbox(self) -> InboxAPI | None: ...

    @property
    def reviews(self) -> ReviewsAPI | None: ...

    @property
    def owners(self) -> OwnersAPI | None: ...

    @property
    def payments(self) -> PaymentsAPI | None: ...

    @property
    def tasks(self) -> TasksAPI | None: ...

    async def aclose(self) -> None: ...


def require_capability(provider: PMSProvider, capability: Capability) -> None:
    """Raise ``CapabilityNotSupportedError`` unless the provider supports ``capability``."""
    if capability not in provider.capabilities:
        raise CapabilityNotSupportedError(provider.provider, capability)


def require_api[A](provider: PMSProvider, capability: Capability, api: A | None) -> A:
    """Check ``capability`` and return the non-None domain API, or raise a client-safe error.

    Usage in a tool: ``api = require_api(p, Capability.LISTINGS_READ, p.listings)``.
    """
    require_capability(provider, capability)
    if api is None:
        raise CapabilityNotSupportedError(provider.provider, capability)
    return api


__all__ = [
    "WRITE_CAPABILITIES",
    "CalendarAPI",
    "Capability",
    "CapabilityNotSupportedError",
    "ConnectionRef",
    "GuestsAPI",
    "InboxAPI",
    "ListingsAPI",
    "OwnersAPI",
    "PMSAuthError",
    "PMSError",
    "PMSNotFoundError",
    "PMSPermissionError",
    "PMSProvider",
    "PMSRateLimitedError",
    "PMSTokenBudgetError",
    "PMSUpstreamError",
    "PMSValidationError",
    "PaymentsAPI",
    "Provider",
    "ReservationsAPI",
    "ReviewsAPI",
    "TasksAPI",
    "call_deadline",
    "remaining_seconds",
    "require_api",
    "require_capability",
]
