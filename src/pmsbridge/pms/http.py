"""The shared HTTP client for PMS APIs: timeouts, safe retries, throttling and a circuit breaker.

One `PMSHttpClient` exists per provider, built for a fixed base URL (a constant here, never user
input). Every call has a connect timeout of 5 seconds and a total timeout of 20 seconds, both capped
by the time left before the tool call's deadline. Rules, per `pmsbridge.pms.base`:

- Reads (GET, HEAD) are retried on 429, 502, 503, 504 and on connection errors or timeouts, with
  exponential backoff and full jitter, using `Retry-After` when the PMS sends it. A retry never
  waits past the call deadline: the call fails with `PMSRateLimitedError` or `PMSUpstreamError`.
- Writes are never retried, except after a 429 that carried `Retry-After` (the PMS did not process
  the request). If a write fails in flight, the error says the outcome is unknown.
- After 5 failed requests in a row the provider's circuit opens for 30 seconds and calls fail
  immediately, which protects both the PMS and the people waiting.
- Errors are mapped to `PMSError` classes with fixed, client-safe messages. Response bodies are
  never put into errors or logs.
"""

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Final
from urllib.parse import unquote

import httpx

from pmsbridge.pms.base import (
    PMSAuthError,
    PMSError,
    PMSNotFoundError,
    PMSPermissionError,
    PMSRateLimitedError,
    PMSUpstreamError,
    PMSValidationError,
    Provider,
    remaining_seconds,
)

logger = logging.getLogger(__name__)

# Fixed addresses. No user-supplied value may change the host that is called.
BASE_URLS: Final[Mapping[Provider, str]] = {Provider.GUESTY: "https://open-api.guesty.com/v1"}
DISPLAY_NAMES: Final[Mapping[Provider, str]] = {
    Provider.GUESTY: "Guesty",
    Provider.HOSTAWAY: "Hostaway",
}

CONNECT_TIMEOUT: Final = 5.0
TOTAL_TIMEOUT: Final = 20.0
MAX_ATTEMPTS: Final = 4
BACKOFF_BASE: Final = 1.0
BACKOFF_CAP: Final = 15.0
MAX_WAIT_WITHOUT_DEADLINE: Final = 30.0
WRITE_RATE_LIMIT_RETRIES: Final = 2
MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024
BREAKER_THRESHOLD: Final = 5
BREAKER_OPEN_SECONDS: Final = 30.0
DEL: Final = 0x7F
RETRYABLE_STATUSES: Final = frozenset(
    {HTTPStatus.BAD_GATEWAY, HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.GATEWAY_TIMEOUT}
)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


# ---- Throttling ----


class TokenBucket:
    """Allows `capacity` requests per `period` seconds, refilled continuously."""

    def __init__(self, capacity: int, period: float, clock: Clock) -> None:
        self._capacity = float(capacity)
        self._rate = capacity / period
        self._clock = clock
        self._tokens = float(capacity)
        self._stamp = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now

    def wait_time(self) -> float:
        """Seconds until one request may go out (0 when one may go now)."""
        self._refill()
        return 0.0 if self._tokens >= 1 else (1 - self._tokens) / self._rate

    def take(self) -> None:
        self._refill()
        self._tokens -= 1

    def clamp(self, remaining: int) -> None:
        """Never believe we have more room than the PMS says it has left."""
        self._refill()
        self._tokens = min(self._tokens, float(remaining))


@dataclass(frozen=True, slots=True)
class RateLimits:
    per_second: int
    per_minute: int
    per_hour: int
    concurrency: int


class Throttle:
    """Per-account limits: three token buckets plus a cap on requests running at once."""

    def __init__(
        self, limits: RateLimits, *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._buckets = (
            ("x-ratelimit-remaining-second", TokenBucket(limits.per_second, 1.0, clock)),
            ("x-ratelimit-remaining-minute", TokenBucket(limits.per_minute, 60.0, clock)),
            ("x-ratelimit-remaining-hour", TokenBucket(limits.per_hour, 3600.0, clock)),
        )
        self._slots = asyncio.Semaphore(limits.concurrency)
        self._sleep = sleep
        self._clock = clock
        self._hold_until = 0.0

    @asynccontextmanager
    async def slot(self) -> AsyncGenerator[None]:
        """Wait for a free slot and for room in every window, never past the call deadline."""
        await self._acquire_slot()
        try:
            await self._wait_for_room()
            yield
        finally:
            self._slots.release()

    async def _acquire_slot(self) -> None:
        limit = remaining_seconds()
        try:
            async with asyncio.timeout(MAX_WAIT_WITHOUT_DEADLINE if limit is None else limit):
                await self._slots.acquire()
        except TimeoutError:
            raise PMSRateLimitedError(
                "Too many requests are already running against this account. Try again shortly.",
                None,
            ) from None

    async def _wait_for_room(self) -> None:
        while True:
            waits = [bucket.wait_time() for _, bucket in self._buckets]
            wait = max(*waits, self._hold_until - self._clock())
            if wait <= 0:
                for _, bucket in self._buckets:
                    bucket.take()
                return
            limit = remaining_seconds()
            if wait > (MAX_WAIT_WITHOUT_DEADLINE if limit is None else limit):
                raise PMSRateLimitedError(
                    f"The request limit for this account was reached. Try again in "
                    f"{math.ceil(wait)} seconds.",
                    wait,
                )
            await self._sleep(wait)

    def hold(self, seconds: float) -> None:
        """Let no request out for `seconds` (the PMS asked us to wait)."""
        self._hold_until = max(self._hold_until, self._clock() + seconds)

    def observe(self, headers: Mapping[str, str]) -> None:
        """Slow down when the PMS reports little room left in a window."""
        for name, bucket in self._buckets:
            value = headers.get(name, "").strip()
            if value.isdecimal():
                bucket.clamp(int(value))


# ---- Circuit breaker ----


class CircuitBreaker:
    """Opens after `threshold` failed requests in a row and lets one probe through afterwards."""

    def __init__(
        self,
        threshold: int = BREAKER_THRESHOLD,
        open_seconds: float = BREAKER_OPEN_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_started: float | None = None

    def retry_in(self) -> float | None:
        """None when a request may go out; otherwise the seconds until the circuit may close."""
        if self._opened_at is None:
            return None
        now = self._clock()
        elapsed = now - self._opened_at
        if elapsed < self._open_seconds:
            return self._open_seconds - elapsed
        if self._probe_started is not None and now - self._probe_started < self._open_seconds:
            return self._open_seconds - (now - self._probe_started)
        self._probe_started = now  # half-open: this request is the probe
        return None

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_started = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._probe_started is not None or self._failures >= self._threshold:
            self._opened_at = self._clock()
            self._probe_started = None


# ---- Responses and errors ----


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes

    def json(self) -> object:
        try:
            return json.loads(self.content)
        except ValueError:
            raise PMSUpstreamError("The PMS returned a response that could not be read.") from None


class _TransportError(Exception):
    """A timeout or connection error. `sent` is False when the request cannot have been received."""

    def __init__(self, sent: bool, timed_out: bool) -> None:
        super().__init__("transport failure")
        self.sent = sent
        self.timed_out = timed_out


def check_path(path: str) -> None:
    """Refuse a path that could point anywhere but one place under the base URL."""
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or any(char in path for char in "?#")
        or any(ord(char) <= ord(" ") or ord(char) == DEL for char in path)
        or _has_dot_segment(path)
    ):
        raise PMSValidationError("The request path is not valid.")


def _has_dot_segment(path: str) -> bool:
    """A `.` or `..` segment or a backslash, also once percent-encoded (even twice)."""
    current = path
    for _ in range(3):
        if "\\" in current or any(part in (".", "..") for part in current.split("/")):
            return True
        current = unquote(current)
    return False


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a `Retry-After` header (a number of seconds or an HTTP date), else None."""
    if value is None:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


_FIXED_ERRORS: Final[Mapping[int, tuple[type[PMSError], str]]] = {
    HTTPStatus.BAD_REQUEST: (PMSValidationError, "{name} rejected the request as invalid."),
    HTTPStatus.UNPROCESSABLE_ENTITY: (
        PMSValidationError,
        "{name} rejected the request as invalid.",
    ),
    HTTPStatus.UNAUTHORIZED: (
        PMSAuthError,
        "{name} did not accept the stored credentials. An administrator needs to check "
        "the connection.",
    ),
    HTTPStatus.FORBIDDEN: (
        PMSPermissionError,
        "{name} denied access: the API key may not allow this action.",
    ),
    HTTPStatus.NOT_FOUND: (PMSNotFoundError, "Not found in {name}."),
}


def is_server_failure(status: int) -> bool:
    return status == HTTPStatus.REQUEST_TIMEOUT or httpx.codes.is_server_error(status)


def error_for_status(provider: Provider, method: str, response: UpstreamResponse) -> PMSError:
    """The client-safe error for a failed response. Never includes any part of the body."""
    name = DISPLAY_NAMES[provider]
    status = response.status_code
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        when = f"in {math.ceil(retry_after)} seconds" if retry_after is not None else "shortly"
        return PMSRateLimitedError(f"{name} rate limit reached. Try again {when}.", retry_after)
    fixed = _FIXED_ERRORS.get(status)
    if fixed is not None:
        return fixed[0](fixed[1].format(name=name))
    unknown = (
        " The change may or may not have been applied: check before trying again."
        if method not in ("GET", "HEAD")
        else ""
    )
    what = "had a problem" if is_server_failure(status) else "returned an unexpected response"
    return PMSUpstreamError(f"{name} {what} (status {status}).{unknown}")


# ---- The client ----


class PMSHttpClient:
    def __init__(
        self,
        provider: Provider,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.provider = provider
        self.name = DISPLAY_NAMES[provider]
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT),
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self._sleep = sleep
        self._jitter = jitter
        self.breaker = breaker or CircuitBreaker()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
        throttle: Throttle | None = None,
        passthrough: frozenset[int] = frozenset(),
    ) -> UpstreamResponse:
        """Send one request. Returns a 2xx response, or one whose status is in `passthrough`.

        `path` is a fixed path under the provider's base URL, such as `/listings/<id>`. Domain code
        must percent-quote every id it puts in a path with `urllib.parse.quote(id, safe="")` and
        pass query values only through `params`.
        """
        check_path(path)
        method = method.upper()
        is_read = method in ("GET", "HEAD")
        wait = self.breaker.retry_in()
        if wait is not None:
            raise PMSUpstreamError(
                f"{self.name} is not answering, so requests are paused. "
                f"Try again in {math.ceil(wait)} seconds."
            )
        attempt = 0
        rate_limited = 0
        while True:
            attempt += 1
            try:
                response = await self._attempt(
                    method=method,
                    path=path,
                    params=params,
                    json_body=json_body,
                    headers=headers,
                    throttle=throttle,
                )
            except _TransportError as failure:
                error = self._transport_error(method, failure)
                if is_read and attempt < MAX_ATTEMPTS:
                    await self._retry_or_raise(error, self._backoff(attempt), breaker=True)
                    continue
                self.breaker.record_failure()
                self._log(method, path, None)
                raise error from None
            status = response.status_code
            if status == HTTPStatus.TOO_MANY_REQUESTS:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                error = error_for_status(self.provider, method, response)
                may_retry = (is_read and attempt < MAX_ATTEMPTS) or (
                    not is_read
                    and retry_after is not None
                    and rate_limited < WRITE_RATE_LIMIT_RETRIES
                )
                if may_retry:
                    rate_limited += 1
                    delay = self._backoff(attempt) if retry_after is None else retry_after
                    await self._retry_or_raise(error, delay, breaker=False)
                    continue
                self._log(method, path, response)
                raise error
            if status in RETRYABLE_STATUSES and is_read and attempt < MAX_ATTEMPTS:
                error = error_for_status(self.provider, method, response)
                await self._retry_or_raise(error, self._backoff(attempt), breaker=True)
                continue
            if is_server_failure(status):
                self.breaker.record_failure()
            else:
                self.breaker.record_success()
            if httpx.codes.is_success(status) or status in passthrough:
                return response
            self._log(method, path, response)
            raise error_for_status(self.provider, method, response)

    def raise_for_status(self, method: str, response: UpstreamResponse) -> None:
        """Raise the mapped error for a passed-through response that is not a success."""
        if not httpx.codes.is_success(response.status_code):
            raise error_for_status(self.provider, method.upper(), response)

    def _backoff(self, attempt: int) -> float:
        return self._jitter() * min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempt - 1))

    async def _retry_or_raise(self, error: PMSError, delay: float, *, breaker: bool) -> None:
        """Wait `delay` seconds and return, or raise `error` when the deadline leaves no time."""
        limit = remaining_seconds()
        if delay >= (MAX_WAIT_WITHOUT_DEADLINE if limit is None else limit):
            if breaker:
                self.breaker.record_failure()
            raise error
        await self._sleep(delay)

    def _transport_error(self, method: str, failure: _TransportError) -> PMSUpstreamError:
        if method in ("GET", "HEAD"):
            what = "timed out" if failure.timed_out else "could not be reached"
            return PMSUpstreamError(f"{self.name} {what}. Try again shortly.")
        if not failure.sent:
            return PMSUpstreamError(
                f"{self.name} could not be reached. Nothing was sent, so it is safe to try again."
            )
        return PMSUpstreamError(
            f"The request to {self.name} "
            f"{'timed out' if failure.timed_out else 'was interrupted'}. "
            "The outcome is unknown: check in the PMS before trying again."
        )

    async def _attempt(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, str | int] | None,
        json_body: object | None,
        headers: Mapping[str, str] | None,
        throttle: Throttle | None,
    ) -> UpstreamResponse:
        async with throttle.slot() if throttle else _no_slot():
            limit = remaining_seconds()
            if limit is not None and limit <= 0:
                raise PMSUpstreamError(
                    "The time allowed for this request ran out before it could be sent."
                )
            total = TOTAL_TIMEOUT if limit is None else min(TOTAL_TIMEOUT, limit)
            request = self._client.build_request(
                method,
                path,
                params=dict(params) if params else None,
                json=json_body,
                headers=dict(headers) if headers else None,
                timeout=httpx.Timeout(total, connect=min(CONNECT_TIMEOUT, total)),
            )
            try:
                async with asyncio.timeout(total):
                    response = await self._send(request)
            except TimeoutError:
                raise _TransportError(sent=True, timed_out=True) from None
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raise _TransportError(sent=False, timed_out=False) from None
            except httpx.TimeoutException:
                raise _TransportError(sent=True, timed_out=True) from None
            except httpx.HTTPError:  # any other failure while sending or reading
                raise _TransportError(sent=True, timed_out=False) from None
            if throttle is not None:
                throttle.observe(response.headers)
                if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    if retry_after is not None:
                        throttle.hold(retry_after)
            return response

    async def _send(self, request: httpx.Request) -> UpstreamResponse:
        response = await self._client.send(request, stream=True)
        too_large = PMSUpstreamError(
            f"{self.name} sent a response that is too large."
            + (
                ""
                if request.method in ("GET", "HEAD")
                else " The outcome is unknown: check in the PMS before trying again."
            )
        )
        try:
            declared = response.headers.get("content-length", "")
            if declared.isdecimal() and int(declared) > MAX_RESPONSE_BYTES:
                raise too_large
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise too_large
                chunks.append(chunk)
            return UpstreamResponse(response.status_code, response.headers, b"".join(chunks))
        finally:
            await response.aclose()

    def _log(self, method: str, path: str, response: UpstreamResponse | None) -> None:
        logger.warning(
            "pms request failed",
            extra={
                "provider": self.provider.value,
                "method": method,
                "path": path,
                "status": response.status_code if response else None,
                "pms_request_id": response.headers.get("x-request-id") if response else None,
            },
        )


@asynccontextmanager
async def _no_slot() -> AsyncGenerator[None]:
    yield


# ---- One client per provider ----

_clients: dict[Provider, PMSHttpClient] = {}


def get_http_client(provider: Provider) -> PMSHttpClient:
    """The shared client (and circuit breaker) for a provider, created on first use."""
    if provider not in BASE_URLS:
        raise PMSValidationError(f"{provider.value} is not available yet.")
    if provider not in _clients:
        _clients[provider] = PMSHttpClient(provider, BASE_URLS[provider])
    return _clients[provider]


async def close_http_clients() -> None:
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        await client.aclose()
