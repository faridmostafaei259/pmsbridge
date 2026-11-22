"""Clients that name themselves with a web address (OAuth Client ID Metadata Documents).

An authorization request whose `client_id` is an https address is answered like this
(`ClientMetadataService.resolve`):

1. The address is checked (`metadata_fetch.validate_client_id_url`).
2. A stored copy of the document that is still fresh is used as it is.
3. Otherwise a signed-in person is needed (the authorization endpoint asks for sign-in first, so
   a stranger never causes a fetch). One fetch at a time, and a caller waits at most 5 seconds
   for its turn. A recent failure for the same address is answered again without a fetch (5
   minutes). At most 30 fetches per hour for the installation, 5 per hour for one host and 10 per
   hour for one person are made; a refused fetch is logged as a warning. Then the document is
   fetched with the guards of `metadata_fetch.py`, checked and stored. The stored copy is used
   until the response's `Cache-Control: max-age` (between 5 minutes and 24 hours; 1 hour when the
   server says nothing) ends, and then fetched again by the next authorization request. Only
   authorization requests fetch: the token endpoint uses the stored copy and never causes an
   outbound request.

The document must repeat the address as `client_id`, name the client, list redirect URIs (checked
like registered ones) and, if it names an authentication method, name `none`: a document is public
and cannot hold a shared secret. Fields pmsbridge does not use are ignored.

The stored client is an ordinary `oauth_clients` row whose key is derived from the address
(`cimd_` and 43 characters), so grants, codes and tokens work as for any other client. Remembering
a failure for 5 minutes keeps a broken or hostile address from costing a fetch on every request;
nothing from the failed response is kept.
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select

from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import clients
from pmsbridge.oauth.clients import RegistrationData
from pmsbridge.oauth.db import OAuthClient
from pmsbridge.oauth.metadata_fetch import (
    TOTAL_TIMEOUT,
    ClientIdUrl,
    MetadataFetcher,
    MetadataFetchError,
    validate_client_id_url,
)
from pmsbridge.oauth.registration import RegistrationError, parse_registration

logger = logging.getLogger("pmsbridge.oauth")

KIND: Final = "metadata_document"
CACHE_MIN: Final = 300
CACHE_MAX: Final = 24 * 3600
CACHE_DEFAULT: Final = 3600
FAILURE_SECONDS: Final = 300
GLOBAL_PER_HOUR: Final = 30
PER_HOST_PER_HOUR: Final = 5
PER_PERSON_PER_HOUR: Final = 10
LOCK_WAIT_SECONDS: Final = TOTAL_TIMEOUT
WINDOW_SECONDS: Final = 3600
MAX_TRACKED: Final = 1000
_FIELDS: Final = ("client_name", "redirect_uris", "grant_types", "response_types")


class FetchRateLimitedError(MetadataFetchError):
    """Too many fetches lately. Not remembered as a failure of the address."""

    def __init__(self) -> None:
        super().__init__(
            "Too many assistant lookups just now. Wait a while and try again.",
            reason="rate_limited",
        )


def derived_client_id(url: str) -> str:
    """The short key of the stored client: stable, and matches the registered id shape."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(url.encode("ascii")).digest())
    return "cimd_" + digest.decode().rstrip("=")


def clamp_seconds(max_age: int | None) -> int:
    return CACHE_DEFAULT if max_age is None else min(max(max_age, CACHE_MIN), CACHE_MAX)


def parse_metadata_document(target: ClientIdUrl, body: bytes) -> RegistrationData:
    """Check the downloaded document. Raises `MetadataFetchError` with a fixed message."""
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise MetadataFetchError("The assistant's document is not valid JSON.") from None
    if not isinstance(document, dict):
        raise MetadataFetchError("The assistant's document must be a JSON object.")
    data: dict[str, object] = {str(key): item for key, item in document.items()}  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
    if data.get("client_id") != target.url:
        raise MetadataFetchError("The document's client_id does not match its web address.")
    if "client_secret" in data or "client_secret_expires_at" in data:
        raise MetadataFetchError("The assistant's document must not contain a client secret.")
    if data.get("token_endpoint_auth_method", "none") != "none":
        raise MetadataFetchError(
            "The assistant's document asks for a client login method that is not supported."
        )
    try:
        return parse_registration({name: data[name] for name in _FIELDS if name in data})
    except RegistrationError as error:
        raise MetadataFetchError(
            f"The assistant's document is not valid: {error.description}"
        ) from None


class FetchLimiter:
    """A sliding hour: fetches in all, for one host and for one person have their own limits."""

    def __init__(
        self,
        *,
        global_limit: int = GLOBAL_PER_HOUR,
        per_host: int = PER_HOST_PER_HOUR,
        per_person: int = PER_PERSON_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._global_limit = global_limit
        self._per_host = per_host
        self._per_person = per_person
        self._clock = clock
        self._global: deque[float] = deque()
        self._hosts: OrderedDict[str, deque[float]] = OrderedDict()
        self._people: OrderedDict[str, deque[float]] = OrderedDict()

    @staticmethod
    def _live(hits: deque[float], now: float) -> int:
        while hits and hits[0] <= now - WINDOW_SECONDS:
            hits.popleft()
        return len(hits)

    @staticmethod
    def _put(table: OrderedDict[str, deque[float]], key: str, hits: deque[float]) -> None:
        if hits:
            table[key] = hits
        while len(table) > MAX_TRACKED:
            table.popitem(last=False)

    def admit(self, host: str, person: int) -> str | None:
        """Count one fetch if allowed (None), else name the limit that refused it."""
        now = self._clock()
        key = str(person)
        host_hits = self._hosts.pop(host, deque[float]())
        person_hits = self._people.pop(key, deque[float]())
        refused = None
        if self._live(self._global, now) >= self._global_limit:
            refused = "global"
        elif self._live(host_hits, now) >= self._per_host:
            refused = "host"
        elif self._live(person_hits, now) >= self._per_person:
            refused = "person"
        else:
            for hits in (self._global, host_hits, person_hits):
                hits.append(now)
        self._put(self._hosts, host, host_hits)
        self._put(self._people, key, person_hits)
        return refused


class ClientMetadataService:
    def __init__(
        self,
        fetcher: MetadataFetcher | None = None,
        *,
        limiter: FetchLimiter | None = None,
        clock: Callable[[], float] = time.monotonic,
        lock_wait: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self._fetcher = fetcher or MetadataFetcher()
        self._limiter = limiter or FetchLimiter(clock=clock)
        self._clock = clock
        self._failures: OrderedDict[str, tuple[float, str, str]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._lock_wait = lock_wait

    async def _fresh(self, url: str) -> OAuthClient | None:
        async with session() as db:
            client = await db.scalar(select(OAuthClient).where(OAuthClient.client_url == url))
        now = datetime.now(UTC)
        if client is None or clients.is_expired(client, now):
            return None
        if client.cache_until is None or client.cache_until <= now:
            return None
        return client

    def _recent_failure(self, url: str) -> MetadataFetchError | None:
        entry = self._failures.get(url)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            del self._failures[url]
            return None
        return MetadataFetchError(entry[1], entry[2])

    def _remember_failure(self, url: str, error: MetadataFetchError) -> None:
        self._failures[url] = (self._clock() + FAILURE_SECONDS, str(error), error.reason)
        self._failures.move_to_end(url)
        while len(self._failures) > MAX_TRACKED:
            self._failures.popitem(last=False)

    async def resolve(self, client_id: str, person: int) -> OAuthClient:
        """The client for this web address, fetched if needed on behalf of a signed-in person.

        Raises `MetadataFetchError` (`FetchRateLimitedError` when a limit or the wait is hit).
        """
        target = validate_client_id_url(client_id)
        if (client := await self._fresh(target.url)) is not None:
            return client
        try:
            await asyncio.wait_for(self._lock.acquire(), self._lock_wait)
        except TimeoutError:
            logger.warning(
                "assistant metadata fetch refused",
                extra={"metadata_host": target.host, "limit": "busy"},
            )
            raise FetchRateLimitedError from None
        try:
            return await self._fetch_locked(target, person)
        finally:
            self._lock.release()

    async def _fetch_locked(self, target: ClientIdUrl, person: int) -> OAuthClient:
        if (client := await self._fresh(target.url)) is not None:
            return client
        if (remembered := self._recent_failure(target.url)) is not None:
            raise remembered
        if (limit := self._limiter.admit(target.host, person)) is not None:
            logger.warning(
                "assistant metadata fetch refused",
                extra={"metadata_host": target.host, "limit": limit},
            )
            raise FetchRateLimitedError
        try:
            fetched = await self._fetcher.fetch(target)
            data = parse_metadata_document(target, fetched.body)
        except MetadataFetchError as error:
            logger.info(
                "assistant metadata failed",
                extra={"metadata_host": target.host, "reason": error.reason},
            )
            self._remember_failure(target.url, error)
            raise
        return await self._store(target, data, clamp_seconds(fetched.max_age))

    async def _store(
        self, target: ClientIdUrl, data: RegistrationData, seconds: int
    ) -> OAuthClient:
        now = datetime.now(UTC)
        key = derived_client_id(target.url)
        async with write_transaction() as db:
            row = await db.get(OAuthClient, key)
            if row is not None and clients.is_expired(row, now):
                await db.delete(row)  # an unused or long idle client starts again
                await db.flush()
                row = None
            if row is None:
                row = OAuthClient(client_id=key, created_at=now, redirect_uris=[], grant_types=[])
                row.client_name = data.name
                db.add(row)
            row.client_name = data.name
            row.redirect_uris = data.redirect_uris
            row.grant_types = data.grant_types
            row.token_endpoint_auth_method = "none"  # noqa: S105  (the method name, not a secret)
            row.kind = KIND
            row.client_url = target.url
            row.fetched_at = now
            row.cache_until = now + timedelta(seconds=seconds)
        return row


_service: ClientMetadataService | None = None


def metadata_service() -> ClientMetadataService:
    global _service  # noqa: PLW0603  (one limiter and one failure memory for the process)
    if _service is None:
        _service = ClientMetadataService()
    return _service


def set_metadata_service(service: ClientMetadataService | None) -> None:
    """Replace the process-wide service (tests; `None` starts a fresh default)."""
    global _service  # noqa: PLW0603
    _service = service
