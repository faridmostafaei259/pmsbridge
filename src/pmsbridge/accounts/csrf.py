"""CSRF protection: masked synchronizer tokens and an Origin check.

The secret behind a token lives on the server (in the session row) or, before sign-in, in a
`__Host-` cookie. The token placed in a page is the secret masked with fresh random bytes, so it
looks different on every response. That keeps the secret safe from compression side channels
(BREACH) even if a proxy compresses the page.
"""

import base64
import binascii
import hmac
import secrets
from typing import Final
from urllib.parse import urlsplit

from starlette.requests import Request

SECRET_BYTES: Final = 32
CSRF_FIELD: Final = "csrf_token"
CSRF_HEADER: Final = "x-csrf-token"
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def new_secret() -> str:
    return secrets.token_hex(SECRET_BYTES)


def mask_token(secret: str) -> str:
    """A token to put in a form. Different on every call, valid for the same secret."""
    raw = bytes.fromhex(secret)
    mask = secrets.token_bytes(len(raw))
    masked = bytes(a ^ b for a, b in zip(raw, mask, strict=True))
    return base64.urlsafe_b64encode(mask + masked).decode("ascii")


def token_matches(secret: str, token: str | None) -> bool:
    """Constant-time check of a submitted token against the secret."""
    if not token:
        return False
    try:
        data = base64.urlsafe_b64decode(token.encode("ascii"))
        expected = bytes.fromhex(secret)
    except (binascii.Error, ValueError):
        return False
    if len(data) != 2 * len(expected):
        return False
    mask, masked = data[: len(expected)], data[len(expected) :]
    unmasked = bytes(a ^ b for a, b in zip(mask, masked, strict=True))
    return hmac.compare_digest(unmasked, expected)


def origin_of(url: str) -> str | None:
    """`scheme://host[:port]` of a URL in lower case, without a default port, or None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None and port != _DEFAULT_PORTS[parts.scheme] else ""
    return f"{parts.scheme}://{host}{suffix}"


def origin_allowed(request: Request, public_base_url: str) -> bool:
    """Origin (or Referer when there is no Origin) must be the public address. Neither: refused."""
    expected = origin_of(public_base_url)
    origin = request.headers.get("origin")
    if origin is not None:
        return expected is not None and origin_of(origin) == expected
    referer = request.headers.get("referer")
    return expected is not None and referer is not None and origin_of(referer) == expected
