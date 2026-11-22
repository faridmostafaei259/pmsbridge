"""One-time codes from an authenticator app (RFC 6238) and single-use recovery codes.

Codes are 6 digits, change every 30 seconds and use HMAC-SHA-1, because that is what every
authenticator app supports. The secret is 160 bits. This module has no storage: see `twofactor`.
"""

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from typing import Final
from urllib.parse import quote

import segno

PERIOD_SECONDS: Final = 30
DIGITS: Final = 6
SECRET_BYTES: Final = 20
WINDOW_STEPS: Final = 1  # the current step plus or minus this many
RECOVERY_CODE_COUNT: Final = 10
RECOVERY_CODE_BYTES: Final = 12  # 96 bits
ISSUER: Final = "pmsbridge"
_TOTP_SHAPE = re.compile(r"[0-9]{6}")
_RECOVERY_SHAPE = re.compile(r"[A-Z2-7]{20}")


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def secret_text(secret: bytes) -> str:
    """The secret as the base32 text people type into an authenticator app."""
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD_SECONDS)


def code_for(secret: bytes, step: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**DIGITS:0{DIGITS}d}"


def is_totp_shaped(text: str) -> bool:
    return _TOTP_SHAPE.fullmatch(text) is not None


def matching_step(secret: bytes, code: str, now: float | None = None) -> int | None:
    """The step whose code equals `code` (within the allowed window), or None.

    Every candidate is compared, in constant time, so the answer does not depend on which matched.
    """
    if not is_totp_shaped(code):
        return None
    centre = current_step(now)
    found: int | None = None
    for step in range(centre - WINDOW_STEPS, centre + WINDOW_STEPS + 1):
        if hmac.compare_digest(code_for(secret, step), code):
            found = step
    return found


def otpauth_uri(secret: bytes, account: str) -> str:
    label = quote(f"{ISSUER}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret_text(secret)}&issuer={quote(ISSUER)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD_SECONDS}"
    )


def qr_svg(data: str) -> str:
    """The QR code as an inline SVG element: no scripts, no inline styles, no external files."""
    return segno.make(data, error="m").svg_inline(  # pyright: ignore[reportUnknownMemberType]
        scale=5,
        border=2,
        dark="#000000",
        light="#ffffff",
        title="QR code for the authenticator app",
    )


def new_recovery_code() -> str:
    """A random code shown as `AAAAA-BBBBB-CCCCC-DDDDD` (96 bits)."""
    text = base64.b32encode(secrets.token_bytes(RECOVERY_CODE_BYTES)).decode("ascii")
    return "-".join(text[i : i + 5] for i in range(0, 20, 5))


def normalise_recovery_code(text: str) -> str | None:
    """Upper-case the code without spaces or hyphens, or None if it cannot be a recovery code."""
    cleaned = re.sub(r"[\s-]", "", text).upper()
    return cleaned if _RECOVERY_SHAPE.fullmatch(cleaned) else None


def hash_recovery_code(normalised: str) -> str:
    return hashlib.sha256(normalised.encode("ascii")).hexdigest()
