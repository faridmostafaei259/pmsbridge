"""Redirect URIs: what may be registered, how a request is matched, what the consent page allows.

A redirect URI must be `https`, or `http` on the loopback interface (`127.0.0.1`, `[::1]` or
`localhost`) for assistants that run on the person's own computer (RFC 8252). It has no fragment
and no login details. A request must name a registered URI exactly. The one exception is the
rule of RFC 8252 section 7.3: for a loopback *address* (`127.0.0.1`, `[::1]`) the port may differ
from the registered one, because desktop assistants pick a free port each time. Nothing else may
differ, and `localhost` by name is matched exactly.
"""

import ipaddress
import re
from typing import Final
from urllib.parse import SplitResult, urlsplit

from pmsbridge.accounts.csrf import origin_of

MAX_URI_LENGTH: Final = 2048
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
LOOPBACK_ADDRESSES: Final = frozenset({"127.0.0.1", "::1"})
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
_SAFE_ORIGIN = re.compile(r"https?://[a-z0-9.:\[\]-]+")


class InvalidRedirectError(ValueError):
    """The redirect URI is not acceptable. The message is safe to show to a developer."""


def _split(uri: str) -> SplitResult:
    try:
        return urlsplit(uri)
    except ValueError:
        raise InvalidRedirectError("is not a valid address") from None


def validate_redirect_uri(uri: object) -> str:
    """Return the URI if it may be registered or requested, else raise `InvalidRedirectError`."""
    if not isinstance(uri, str) or not uri:
        raise InvalidRedirectError("must be a non-empty string")
    if len(uri) > MAX_URI_LENGTH:
        raise InvalidRedirectError("is too long")
    if not uri.isascii() or not uri.isprintable() or " " in uri or "\\" in uri:
        raise InvalidRedirectError("contains characters that are not allowed")
    if "#" in uri:
        raise InvalidRedirectError("must not contain a fragment")
    parts = _split(uri)
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        raise InvalidRedirectError("must be an https address (no login details)")
    if "%" in parts.netloc:
        raise InvalidRedirectError("has an invalid host name")
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise InvalidRedirectError("has an invalid port") from None
    if port == 0:
        raise InvalidRedirectError("has an invalid port")
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise InvalidRedirectError("must use https unless it points to this computer")
    if not _is_ip(host) and not _HOSTNAME.fullmatch(host):
        raise InvalidRedirectError("has an invalid host name")
    return uri


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _loopback_key(uri: str) -> tuple[str, str, str] | None:
    """Host, path and query of an `http` loopback-address URI; None for any other URI."""
    parts = _split(uri)
    host = parts.hostname or ""
    if parts.scheme == "http" and host in LOOPBACK_ADDRESSES:
        return host, parts.path, parts.query
    return None


def redirect_matches(requested: str, registered: list[str]) -> bool:
    """True if the requested URI equals a registered one (loopback ports may differ)."""
    if requested in registered:
        return True
    try:
        validate_redirect_uri(requested)
        key = _loopback_key(requested)
    except InvalidRedirectError:
        return False
    return key is not None and any(_safe_loopback_key(candidate) == key for candidate in registered)


def _safe_loopback_key(uri: str) -> tuple[str, str, str] | None:
    try:
        return _loopback_key(uri)
    except InvalidRedirectError:
        return None


def redirect_origin(uri: str) -> str:
    """`scheme://host[:port]` of a validated redirect URI, safe to place in a CSP header."""
    origin = origin_of(uri)
    if origin is None or not _SAFE_ORIGIN.fullmatch(origin):
        raise InvalidRedirectError("has no usable origin")
    return origin


def redirect_host(uri: str) -> str:
    """The host to show a person ("example.com", "127.0.0.1", "[::1]"), without the port."""
    host = _split(uri).hostname or ""
    return f"[{host}]" if ":" in host else host
