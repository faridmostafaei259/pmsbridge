"""Canonical, provider-neutral data models.

MCP tools only ever see these models. Each provider adapter maps its own API payloads to them.

Rules for every model here:
- Immutable (frozen) and strict about unknown fields.
- No provider names, URLs or raw payloads.
- Money is always a ``Money`` value, never a bare float.
- Dates without a time are ``datetime.date``; instants are timezone-aware ``datetime``.

Read models versus write inputs:
- Read models (data coming from a PMS) never reject long text. Over-long text is truncated and
  marked with ``TRUNCATION_MARKER``, so one unusual record cannot fail a whole page.
- Write inputs (data going to a PMS) use hard limits and reject invalid values.

Adapter rules for read models:
- If the PMS leaves a required display name empty, use ``UNKNOWN_NAME``.
- ``extras`` holds only scalar, non-secret values (at most ``MAX_EXTRAS_KEYS``), never raw payloads.
  Tools must never read named keys from ``extras``; it exists for display only.

Cursor rules:
- Cursors are opaque strings created by adapters and echoed back by clients. They must never contain
  URLs or host names. Adapters parse them strictly and raise ``PMSValidationError`` when malformed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
MAX_CALENDAR_RANGE_DAYS = 366
MAX_CALENDAR_UPDATES = 31
MAX_PRICE_DECIMALS = 2
MAX_SHORT_TEXT = 500
MAX_TEXT_LENGTH = 5000
MAX_EXTRAS_KEYS = 50
TRUNCATION_MARKER = " [truncated]"
UNKNOWN_NAME = "Unknown"


def _truncate(limit: int) -> Callable[[object], object]:
    def apply(value: object) -> object:
        if isinstance(value, str) and len(value) > limit:
            return value[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
        return value

    return apply


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

# Read-side text: truncated, never rejected.
Text = Annotated[str, BeforeValidator(_truncate(MAX_SHORT_TEXT))]
LongText = Annotated[str, BeforeValidator(_truncate(MAX_TEXT_LENGTH))]

# Write-side text: hard limits.
InputText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT_LENGTH)]
InputTitle = Annotated[str, StringConstraints(min_length=1, max_length=200)]

ExtraValue = Text | int | float | bool | None


def _check_extras(value: dict[str, ExtraValue]) -> dict[str, ExtraValue]:
    if len(value) > MAX_EXTRAS_KEYS:
        raise ValueError(f"extras may hold at most {MAX_EXTRAS_KEYS} keys")
    return value


Extras = Annotated[dict[str, ExtraValue], AfterValidator(_check_extras)]


def _no_extras() -> dict[str, ExtraValue]:
    return {}


class CanonicalModel(BaseModel):
    """Base for all canonical models."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- shared values


class Money(CanonicalModel):
    """An amount in a currency. Read values may be zero or negative (refunds, discounts)."""

    amount: Decimal
    currency: CurrencyCode


class Address(CanonicalModel):
    line1: Text | None = None
    line2: Text | None = None
    city: Text | None = None
    state: Text | None = None
    postal_code: Text | None = None
    country: Text | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class PageRequest(CanonicalModel):
    """Cursor-based paging.

    The cursor is opaque to callers. See the module docstring for cursor rules.
    """

    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    cursor: Annotated[str, StringConstraints(max_length=1000)] | None = None


class Page[T](CanonicalModel):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = Field(default=None, ge=0)


# --------------------------------------------------------------------------- listings


class ListingStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class Listing(CanonicalModel):
    id: Identifier
    title: Text
    nickname: Text | None = None
    status: ListingStatus = ListingStatus.UNKNOWN
    property_type: Text | None = None
    address: Address | None = None
    bedrooms: int | None = Field(default=None, ge=0)
    bathrooms: float | None = Field(default=None, ge=0)
    max_guests: int | None = Field(default=None, ge=0)
    timezone: Text | None = None
    base_price: Money | None = None
    check_in_time: Text | None = None
    check_out_time: Text | None = None
    tags: list[Text] = Field(default_factory=list[Text])
    extras: Extras = Field(default_factory=_no_extras)


class ListingQuery(PageRequest):
    active_only: bool | None = None
    search: Text | None = None


# --------------------------------------------------------------------------- guests


class GuestSummary(CanonicalModel):
    """Guest details embedded in a reservation.

    Some providers keep guest contact details only on the reservation, so tools must use this for
    reservation contact details and must not depend on ``GuestsAPI`` for them.
    """

    name: Text = UNKNOWN_NAME
    email: Text | None = None
    phone: Text | None = None


class Guest(CanonicalModel):
    id: Identifier
    full_name: Text = UNKNOWN_NAME
    first_name: Text | None = None
    last_name: Text | None = None
    email: Text | None = None
    phone: Text | None = None
    language: Text | None = None
    extras: Extras = Field(default_factory=_no_extras)


class GuestQuery(PageRequest):
    search: Text | None = None


# --------------------------------------------------------------------------- reservations


class ReservationStatus(StrEnum):
    INQUIRY = "inquiry"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    CANCELLED = "cancelled"
    DECLINED = "declined"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class Reservation(CanonicalModel):
    id: Identifier
    listing_id: Identifier
    status: ReservationStatus = ReservationStatus.UNKNOWN
    channel: Text | None = None
    confirmation_code: Text | None = None
    guest_id: Identifier | None = None
    guest: GuestSummary | None = None
    check_in: date
    check_out: date
    nights: int = Field(ge=0)
    guests_count: int | None = Field(default=None, ge=0)
    total_price: Money | None = None
    balance_due: Money | None = None
    created_at: AwareDatetime | None = None
    notes: LongText | None = None
    extras: Extras = Field(default_factory=_no_extras)

    @model_validator(mode="after")
    def _check_dates(self) -> Reservation:
        if self.check_out < self.check_in:
            raise ValueError("check_out must not be before check_in")
        if self.nights != (self.check_out - self.check_in).days:
            raise ValueError("nights must equal the number of days between check_in and check_out")
        return self


class ReservationQuery(PageRequest):
    listing_id: Identifier | None = None
    statuses: list[ReservationStatus] | None = None
    search: Text | None = Field(default=None, description="Guest name or confirmation code")
    check_in_from: date | None = None
    check_in_to: date | None = None
    check_out_from: date | None = None
    check_out_to: date | None = None
    created_from: date | None = None
    created_to: date | None = None

    @model_validator(mode="after")
    def _check_ranges(self) -> ReservationQuery:
        for start, end, name in (
            (self.check_in_from, self.check_in_to, "check_in"),
            (self.check_out_from, self.check_out_to, "check_out"),
            (self.created_from, self.created_to, "created"),
        ):
            if start and end and end < start:
                raise ValueError(f"{name}_to must not be before {name}_from")
        return self


class ReservationNote(CanonicalModel):
    """A note to append to a reservation. Adapters append; they never overwrite existing notes."""

    reservation_id: Identifier
    note: InputText


# --------------------------------------------------------------------------- calendar


class CalendarDay(CanonicalModel):
    listing_id: Identifier
    day: date
    available: bool
    price: Money | None = None
    min_nights: int | None = Field(default=None, ge=0)
    reservation_id: Identifier | None = None
    blocked_reason: Text | None = None
    extras: Extras = Field(default_factory=_no_extras)


class CalendarRange(CanonicalModel):
    listing_id: Identifier
    start: date
    end: date

    @model_validator(mode="after")
    def _check_range(self) -> CalendarRange:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        if (self.end - self.start).days >= MAX_CALENDAR_RANGE_DAYS:
            raise ValueError(f"range must be shorter than {MAX_CALENDAR_RANGE_DAYS} days")
        return self


class CalendarUpdate(CanonicalModel):
    """A change applied to every day in [start, end] (inclusive). At least one field must be set.

    Prices must be positive with at most two decimal places. Ranges that ended before yesterday are
    rejected (one day of tolerance covers listing time zones ahead of the server).
    """

    start: date
    end: date
    price: Money | None = None
    min_nights: int | None = Field(default=None, ge=1, le=365)
    available: bool | None = None

    @model_validator(mode="after")
    def _check_update(self) -> CalendarUpdate:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        if (self.end - self.start).days >= MAX_CALENDAR_RANGE_DAYS:
            raise ValueError(f"range must be shorter than {MAX_CALENDAR_RANGE_DAYS} days")
        if self.end < date.today() - timedelta(days=1):
            raise ValueError("cannot change calendar days in the past")
        if self.price is None and self.min_nights is None and self.available is None:
            raise ValueError("at least one of price, min_nights or available must be set")
        if self.price is not None:
            if self.price.amount <= 0:
                raise ValueError("price must be greater than zero")
            exponent = self.price.amount.as_tuple().exponent
            if isinstance(exponent, int) and exponent < -MAX_PRICE_DECIMALS:
                raise ValueError(f"price may have at most {MAX_PRICE_DECIMALS} decimal places")
        return self


class CalendarUpdateRequest(CanonicalModel):
    listing_id: Identifier
    updates: list[CalendarUpdate] = Field(min_length=1, max_length=MAX_CALENDAR_UPDATES)


# --------------------------------------------------------------------------- inbox


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    SYSTEM = "system"


class MessageChannel(StrEnum):
    """How a message is delivered. ``DEFAULT`` means: reply on the conversation's own channel."""

    DEFAULT = "default"
    PLATFORM = "platform"
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class Conversation(CanonicalModel):
    id: Identifier
    reservation_id: Identifier | None = None
    listing_id: Identifier | None = None
    guest_id: Identifier | None = None
    guest_name: Text | None = None
    channel: Text | None = None
    available_channels: list[MessageChannel] = Field(default_factory=list[MessageChannel])
    last_message_at: AwareDatetime | None = None
    unread: bool | None = None
    extras: Extras = Field(default_factory=_no_extras)


class ConversationQuery(PageRequest):
    reservation_id: Identifier | None = None
    listing_id: Identifier | None = None
    unread_only: bool | None = None


class Message(CanonicalModel):
    id: Identifier
    conversation_id: Identifier
    direction: MessageDirection
    body: LongText
    sent_at: AwareDatetime | None = None
    channel: Text | None = None
    author_name: Text | None = None


class OutgoingMessage(CanonicalModel):
    conversation_id: Identifier
    body: InputText
    channel: MessageChannel = MessageChannel.DEFAULT


# --------------------------------------------------------------------------- reviews


class Review(CanonicalModel):
    id: Identifier
    listing_id: Identifier | None = None
    reservation_id: Identifier | None = None
    channel: Text | None = None
    rating: float | None = Field(default=None, ge=0)
    rating_scale: float | None = Field(default=None, gt=0)
    public_text: LongText | None = None
    private_text: LongText | None = None
    reply: LongText | None = None
    created_at: AwareDatetime | None = None
    extras: Extras = Field(default_factory=_no_extras)


class ReviewQuery(PageRequest):
    listing_id: Identifier | None = None
    since: date | None = None


class ReviewReply(CanonicalModel):
    review_id: Identifier
    body: InputText


# --------------------------------------------------------------------------- owners


class Owner(CanonicalModel):
    id: Identifier
    full_name: Text = UNKNOWN_NAME
    email: Text | None = None
    phone: Text | None = None
    listing_ids: list[Identifier] = Field(default_factory=list[Identifier])
    extras: Extras = Field(default_factory=_no_extras)


# --------------------------------------------------------------------------- payments


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Payment(CanonicalModel):
    id: Identifier
    reservation_id: Identifier
    amount: Money
    status: PaymentStatus = PaymentStatus.UNKNOWN
    method: Text | None = None
    paid_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
    extras: Extras = Field(default_factory=_no_extras)


# --------------------------------------------------------------------------- tasks


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Task(CanonicalModel):
    id: Identifier
    title: Text
    description: LongText | None = None
    listing_id: Identifier | None = None
    reservation_id: Identifier | None = None
    status: TaskStatus = TaskStatus.UNKNOWN
    due_at: AwareDatetime | None = None
    assignee_name: Text | None = None
    extras: Extras = Field(default_factory=_no_extras)


class TaskQuery(PageRequest):
    listing_id: Identifier | None = None
    status: TaskStatus | None = None
    due_from: date | None = None
    due_to: date | None = None

    @model_validator(mode="after")
    def _check_range(self) -> TaskQuery:
        if self.due_from and self.due_to and self.due_to < self.due_from:
            raise ValueError("due_to must not be before due_from")
        return self


class NewTask(CanonicalModel):
    title: InputTitle
    description: InputText | None = None
    listing_id: Identifier | None = None
    reservation_id: Identifier | None = None
    due_at: AwareDatetime | None = None


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_CALENDAR_RANGE_DAYS",
    "MAX_CALENDAR_UPDATES",
    "MAX_EXTRAS_KEYS",
    "MAX_PAGE_SIZE",
    "MAX_PRICE_DECIMALS",
    "MAX_SHORT_TEXT",
    "MAX_TEXT_LENGTH",
    "TRUNCATION_MARKER",
    "UNKNOWN_NAME",
    "Address",
    "CalendarDay",
    "CalendarRange",
    "CalendarUpdate",
    "CalendarUpdateRequest",
    "CanonicalModel",
    "Conversation",
    "ConversationQuery",
    "Guest",
    "GuestQuery",
    "GuestSummary",
    "Listing",
    "ListingQuery",
    "ListingStatus",
    "Message",
    "MessageChannel",
    "MessageDirection",
    "Money",
    "NewTask",
    "OutgoingMessage",
    "Owner",
    "Page",
    "PageRequest",
    "Payment",
    "PaymentStatus",
    "Reservation",
    "ReservationNote",
    "ReservationQuery",
    "ReservationStatus",
    "Review",
    "ReviewQuery",
    "ReviewReply",
    "Task",
    "TaskQuery",
    "TaskStatus",
]
