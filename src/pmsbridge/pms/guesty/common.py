"""Helpers shared by the Guesty domain modules: id quoting, safe value readers, the skip cursor.

Domain modules import from here and never from each other. Guesty payloads are untrusted, so every
reader returns None for a value of the wrong type instead of raising.
"""

import base64
import binascii
import math
import re
from typing import Final, cast
from urllib.parse import quote

from pmsbridge.pms.base import PMSValidationError

MAX_ID_LENGTH: Final = 200
MAX_SKIP: Final = 100_000
_CURSOR_PATTERN: Final = re.compile(r"^skip=(\d{1,6})$")


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
