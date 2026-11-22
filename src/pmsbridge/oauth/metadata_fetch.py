"""Fetching a client's metadata document: the address rules and the guarded HTTPS request.

An AI assistant may name itself with an https address (its `client_id`); pmsbridge then downloads
the JSON document at that address (OAuth Client ID Metadata Documents, MCP authorization
specification 2026-07-28). The address is chosen by whoever sends the authorization request, so
this is the one outbound request a stranger can cause. It is limited as follows (ADR 0011):

* The address is `https`, port 443 only, a real host name (never an IP address), a path, no query,
  fragment, `;` or login details, ASCII only, at most 1024 characters (`validate_client_id_url`).
* The host name is resolved here. If ANY resolved address is not a public one (private, loopback,
  link-local, multicast, reserved, unspecified, carrier-grade NAT, the IPv4-in-IPv6 forms, the
  cloud metadata addresses) the fetch is refused (`is_public_address`).
* The connection then goes to a vetted address only (`PinnedBackend`), so a second DNS answer
  cannot swap in another one. TLS is started with the original host name for the server name
  indication and the certificate check; the `Host` header is the original host name too.
* No redirects (a 3xx fails), 5 seconds in total including the name lookup, 3 seconds to connect,
  a body of at most 16 KiB (reading stops past it), status 200 with a JSON content type only.
* The body is never logged: a fetch logs the host, the status class and the duration only, and a
  failure adds one fixed reason code (`MetadataFetchError.reason`).

Nothing here touches the database; `client_metadata.py` adds limits, caching and storage.
"""

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import unquote, urlsplit

import certifi
import httpcore

logger = logging.getLogger("pmsbridge.oauth")

MAX_URL_LENGTH: Final = 1024  # fits in the authorize query cap, with room for other parameters
MAX_BODY_BYTES: Final = 16 * 1024
TOTAL_TIMEOUT: Final = 5.0
CONNECT_TIMEOUT: Final = 3.0
MAX_ADDRESSES: Final = 8
HTTPS_PORT: Final = 443

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TLD = r"(?:[a-z]{2,63}|xn--[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)"
_HOST = re.compile(rf"(?:{_LABEL}\.)+{_TLD}")

_DENIED_V4: Final = tuple(
    ipaddress.IPv4Network(net)
    for net in (
        "0.0.0.0/8",  # this network, unspecified
        "10.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, includes the cloud metadata address 169.254.169.254
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, includes the broadcast address
    )
)
_DENIED_V6: Final = tuple(
    ipaddress.IPv6Network(net)
    for net in (
        "::/96",  # unspecified, loopback and the deprecated IPv4-compatible form
        "::ffff:0:0/96",  # IPv4-mapped
        "64:ff9b::/96",  # NAT64 (embeds an IPv4 address)
        "64:ff9b:1::/48",
        "100::/64",  # discard-only
        "2001::/23",  # protocol assignments, Teredo
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds an IPv4 address)
        "3fff::/20",  # documentation
        "fc00::/7",  # unique local, includes the cloud metadata address fd00:ec2::254
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated)
        "ff00::/8",  # multicast
    )
)

# The socket option shapes httpcore accepts (its own alias is private).
SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)
_PATH_MIN: Final = 2
_MAX_AGE_DIGITS: Final = 10
_OK: Final = 200
_REDIRECT_FIRST: Final = 300
_REDIRECT_END: Final = 400
_IPV4: Final = 4

# IPv6 addresses are accepted only inside global unicast space; the denials above then apply.
_GLOBAL_V6: Final = ipaddress.IPv6Network("2000::/3")

Resolver = Callable[[str], Awaitable[list[str]]]


class MetadataFetchError(Exception):
    """The document could not be used. The message is fixed text, safe to show to a person.

    `reason` is a fixed code for the logs: redirect, http_error, not_json, encoding, too_large,
    private_address, dns, timeout, tls, invalid_address, invalid_document or rate_limited.
    """

    def __init__(self, message: str, reason: str = "invalid_document") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ClientIdUrl:
    url: str
    host: str


@dataclass(frozen=True)
class FetchedDocument:
    body: bytes
    max_age: int | None  # seconds from Cache-Control; None when the server gave none


def is_url_client_id(client_id: str) -> bool:
    """True for a metadata-document client id. Registered client ids never contain a colon."""
    return ":" in client_id


def validate_client_id_url(value: str) -> ClientIdUrl:
    """Return the checked address, or raise `MetadataFetchError`."""

    def refuse(reason: str) -> MetadataFetchError:
        return MetadataFetchError(
            f"The assistant's web address {reason}.", reason="invalid_address"
        )

    if not value.startswith("https://"):
        raise refuse("must start with https://")
    if len(value) > MAX_URL_LENGTH:
        raise refuse("is too long")
    if not value.isascii() or not value.isprintable() or " " in value or "\\" in value:
        raise refuse("contains characters that are not allowed")
    if "#" in value or "?" in value or ";" in value:
        raise refuse("must not contain a query, fragment or parameters")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise refuse("is not valid") from None
    host = parts.hostname or ""
    if "@" in parts.netloc or parts.netloc.removesuffix(f":{HTTPS_PORT}") != host:
        raise refuse("must be a plain lower-case host name, with no login details")
    if port not in (None, HTTPS_PORT):
        raise refuse("must use port 443")
    if not _HOST.fullmatch(host):
        raise refuse("must use a host name, not an IP address")
    if len(parts.path) < _PATH_MIN:
        raise refuse("must include a path")
    if any(unquote(segment) in (".", "..") for segment in parts.path.split("/")):
        raise refuse("must not contain . or .. path segments")
    return ClientIdUrl(value, host)


def is_public_address(raw: str) -> bool:
    """True only for an address on the public internet (never a private or special one)."""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if address.version != _IPV4 and address not in _GLOBAL_V6:
        return False
    denied: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network] = (
        _DENIED_V4 if address.version == _IPV4 else _DENIED_V6
    )
    if any(address in net for net in denied):  # pyright: ignore[reportOperatorIssue]
        return False
    return address.is_global


async def system_resolver(host: str) -> list[str]:
    """Every address the system resolver returns for `host` (IPv4 first).

    The lookup runs in a thread that a timeout cannot stop, so a name server that never answers
    leaves a thread behind. That is bounded by the hourly fetch limits (`client_metadata.py`).
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, HTTPS_PORT, type=socket.SOCK_STREAM
        )
    except OSError:
        raise MetadataFetchError(
            "The assistant's web address could not be found.", reason="dns"
        ) from None
    found = list(dict.fromkeys(str(info[4][0]) for info in infos))
    return sorted(found, key=lambda raw: ipaddress.ip_address(raw).version)


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connects only to the vetted addresses of one host, whatever name the pool asks for."""

    def __init__(
        self, inner: httpcore.AsyncNetworkBackend, host: str, addresses: list[str]
    ) -> None:
        self._inner = inner
        self._host = host
        self._addresses = addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109  (the base class signature)
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host != self._host or port != HTTPS_PORT:
            raise httpcore.ConnectError("unexpected destination")
        failure: Exception = httpcore.ConnectError("no address")
        for address in self._addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                failure = error
        raise failure

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109  (the base class signature)
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def parse_max_age(header: str | None) -> int | None:
    """Seconds a response may be reused (`Cache-Control`); 0 for no-store or no-cache."""
    if header is None:
        return None
    seconds: int | None = None
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        name = name.strip().lower()
        if name in ("no-store", "no-cache"):
            return 0
        value = value.strip().strip('"')
        if (
            name == "max-age"
            and seconds is None
            and value.isdecimal()
            and len(value) <= _MAX_AGE_DIGITS
        ):
            seconds = int(value)
    return seconds


def _is_json(content_type: str) -> bool:
    media = content_type.split(";", maxsplit=1)[0].strip().lower()
    return media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )


def _header_map(headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, value in headers:
        found.setdefault(name.decode("latin-1").lower(), value.decode("latin-1"))
    return found


class MetadataFetcher:
    """Downloads one client metadata document with every guard of this module applied."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        backend: httpcore.AsyncNetworkBackend | None = None,
        total_timeout: float = TOTAL_TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
        max_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._resolver = resolver
        self._backend = backend
        self._total_timeout = total_timeout
        self._connect_timeout = connect_timeout
        self._max_bytes = max_bytes
        self._ssl: ssl.SSLContext | None = None

    def _context(self) -> ssl.SSLContext:
        if self._ssl is None:
            context = ssl.create_default_context(cafile=certifi.where())
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self._ssl = context  # verifies the certificate against the host name
        return self._ssl

    async def vetted_addresses(self, host: str) -> list[str]:
        answers = await self._resolver(host)
        # Every answer is checked; only then are the first few kept for connecting.
        if not answers or not all(is_public_address(address) for address in answers):
            raise MetadataFetchError(
                "The assistant's web address points to a network that pmsbridge does not contact.",
                reason="private_address",
            )
        return answers[:MAX_ADDRESSES]

    async def fetch(self, target: ClientIdUrl) -> FetchedDocument:
        started = time.monotonic()
        status_class = "none"
        try:
            async with asyncio.timeout(self._total_timeout):
                addresses = await self.vetted_addresses(target.host)
                document, status = await self._get(target, addresses)
            status_class = f"{status // 100}xx"
            return document
        except TimeoutError:
            raise MetadataFetchError(
                "The assistant's web address did not answer in time.", reason="timeout"
            ) from None
        finally:
            logger.info(
                "assistant metadata fetch",
                extra={
                    "metadata_host": target.host,
                    "status_class": status_class,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )

    async def _get(self, target: ClientIdUrl, addresses: list[str]) -> tuple[FetchedDocument, int]:
        network = PinnedBackend(
            self._backend or cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend()),
            target.host,
            addresses,
        )
        timeouts = {
            "connect": self._connect_timeout,
            "read": self._total_timeout,
            "write": self._total_timeout,
            "pool": self._total_timeout,
        }
        headers = [
            (b"Host", target.host.encode("ascii")),
            (b"Accept", b"application/json"),
            (b"Accept-Encoding", b"identity"),
            (b"User-Agent", b"pmsbridge"),
        ]
        try:
            async with (
                httpcore.AsyncConnectionPool(
                    ssl_context=self._context(),
                    max_connections=1,
                    retries=0,
                    http2=False,
                    network_backend=network,
                ) as pool,
                pool.stream(
                    "GET", target.url, headers=headers, extensions={"timeout": timeouts}
                ) as response,
            ):
                sent = _header_map(response.headers)
                self._check_response(response.status, sent)
                body = bytearray()
                async for chunk in response.aiter_stream():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise MetadataFetchError(
                            "The assistant's document is too large.", reason="too_large"
                        )
        except MetadataFetchError:
            raise
        except httpcore.TimeoutException:
            raise MetadataFetchError(
                "The assistant's web address did not answer in time.", reason="timeout"
            ) from None
        except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.UnsupportedProtocol):
            raise MetadataFetchError(
                "The assistant's web address could not be reached securely.", reason="tls"
            ) from None
        return FetchedDocument(
            bytes(body), parse_max_age(sent.get("cache-control"))
        ), response.status

    def _check_response(self, status: int, sent: dict[str, str]) -> None:
        if _REDIRECT_FIRST <= status < _REDIRECT_END:
            raise MetadataFetchError(
                "The assistant's web address redirects, which is not allowed.", reason="redirect"
            )
        if status != _OK:
            raise MetadataFetchError(
                f"The assistant's web address answered with an error ({status // 100}xx).",
                reason="http_error",
            )
        if not _is_json(sent.get("content-type", "")):
            raise MetadataFetchError(
                "The assistant's web address did not return JSON.", reason="not_json"
            )
        if sent.get("content-encoding", "identity").strip().lower() != "identity":
            raise MetadataFetchError(
                "The assistant's web address used an unsupported encoding.", reason="encoding"
            )
        length = sent.get("content-length", "")
        if length.isdecimal() and int(length) > self._max_bytes:
            raise MetadataFetchError("The assistant's document is too large.", reason="too_large")
