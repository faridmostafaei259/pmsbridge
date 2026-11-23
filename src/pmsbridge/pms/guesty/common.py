"""Helpers shared by the Guesty domain modules: id quoting, safe value readers, the skip cursor.

Domain modules import from here and never from each other. Guesty payloads are untrusted, so every
reader returns None for a value of the wrong type instead of raising.
"""

import base64
import binascii
import math
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, cast
from urllib.parse import quote

from pmsbridge.pms.base import PMSValidationError
from pmsbridge.pms.models import Money

MAX_ID_LENGTH: Final = 200
MAX_SKIP: Final = 100_000
_CURSOR_PATTERN: Final = re.compile(r"^skip=(\d{1,6})$")
_DATE_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_PATTERN: Final = re.compile(r"^[A-Z]{3}$")
MAX_TOKEN_LENGTH: Final = 500
TOKEN_CURSOR_PREFIX: Final = "t."  # noqa: S105  (a cursor marker, not a password)
_TOKEN_PATTERN: Final = re.compile(r"^[\x21-\x7e]{1,500}$")


def quote_id(value: str) -> str:
    """An id, checked and percent-quoted so it can only ever be one path segment."""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_ID_LENGTH:
        raise PMSValidationError("The id is not valid.")
    return quote(cleaned, safe="")


def as_dict(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def as_list(value: object) -> list[object] | None:
    return cast("list[object]", value) if isinstance(value, list) else None


def text_of(value: object) -> str | None:
    """A non-empty string, trimmed; anything else is None."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def number_of(value: object) -> int | float | None:
    """A finite number (never a boolean), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def flag_of(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def count_of(value: object) -> int | None:
    number = number_of(value)
    return int(number) if number is not None and number >= 0 else None


def date_of(value: object) -> date | None:
    """A calendar date written exactly as YYYY-MM-DD, else None."""
    text = text_of(value)
    if text is None or not _DATE_PATTERN.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def datetime_of(value: object, *, naive_as_utc: bool = False) -> datetime | None:
    """An ISO 8601 instant as an aware datetime.

    A bare date is always None. A datetime without an offset is None unless `naive_as_utc` is set,
    which reads it as UTC. Only set it where Guesty documents the value as UTC (for example
    `createdAt`). Never set it for Guesty's listing-local datetimes that carry no offset (such as
    the reports `checkIn`): reading those as UTC would be wrong by the listing's time zone.
    """
    text = text_of(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    if not naive_as_utc or len(text) <= len("YYYY-MM-DD"):
        return None
    return parsed.replace(tzinfo=UTC)


def money_of(amount: object, currency: object) -> Money | None:
    """An amount (a finite JSON number) and a three-letter currency code (any case), else None."""
    value, text = number_of(amount), text_of(currency)
    code = text.upper() if text is not None else None
    if value is None or code is None or not _CURRENCY_PATTERN.match(code):
        return None
    return Money(amount=Decimal(str(value)), currency=code)


def strings_of(value: object, *, limit: int) -> list[str]:
    items = as_list(value)
    if items is None:
        return []
    found = [text for item in items if (text := text_of(item)) is not None]
    return found[:limit]


def encode_cursor(skip: int) -> str:
    """An opaque cursor that records how many items to skip."""
    return base64.urlsafe_b64encode(f"skip={skip}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """The number of items to skip. A cursor that this adapter did not make is refused."""
    if cursor is None:
        return 0
    invalid = PMSValidationError("Invalid pagination cursor.")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise invalid from None
    match = _CURSOR_PATTERN.match(raw)
    if match is None or encode_cursor(int(match.group(1))) != cursor:
        raise invalid
    skip = int(match.group(1))
    if skip > MAX_SKIP:
        raise invalid
    return skip


def _usable_token(token: str) -> bool:
    """A paging token Guesty made: short printable text with no spaces and no URL in it."""
    return (
        len(token) <= MAX_TOKEN_LENGTH and bool(_TOKEN_PATTERN.match(token)) and "://" not in token
    )


def encode_token_cursor(token: object) -> str | None:
    """An opaque cursor that wraps a paging token from Guesty, or None when there is no usable one.

    For Guesty lists that page with `cursorAfter`. The token is only ever sent back as a query
    value; an unusable one (empty, long, with spaces or a URL) ends the paging instead.
    """
    text = text_of(token)
    if text is None or not _usable_token(text):
        return None
    return TOKEN_CURSOR_PREFIX + base64.urlsafe_b64encode(text.encode("ascii")).decode().rstrip("=")


def decode_token_cursor(cursor: str | None) -> str | None:
    """The Guesty paging token in a cursor from `encode_token_cursor`; any other is refused."""
    if cursor is None:
        return None
    invalid = PMSValidationError("Invalid pagination cursor.")
    if not cursor.startswith(TOKEN_CURSOR_PREFIX):
        raise invalid
    encoded = cursor[len(TOKEN_CURSOR_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise invalid from None
    if not _usable_token(raw) or encode_token_cursor(raw) != cursor:
        raise invalid
    return raw
