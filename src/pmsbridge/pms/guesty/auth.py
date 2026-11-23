"""Guesty access tokens, with the daily token budget protected.

Guesty allows only 5 new access tokens per 24 hours for each API key. Running out locks the company
out of Guesty until a slot frees up, so this is the ONLY code that may call `/oauth2/token`, and it
does so only through the locked refresh path below:

1. `get_token` reads the stored token (encrypted in the database, so it survives restarts). A token
   that is valid and outside the refresh window is returned with no other work.
2. Otherwise one caller at a time (a per-connection lock, ADR 0004) re-reads the stored token in a
   short read, because another caller may just have refreshed it, and only then asks Guesty for a
   token with no database transaction open. The result is stored in a short write. The request and
   the store run as one task per connection that a cancelled or timed-out caller does not stop, so
   a token Guesty issued is never lost. The token request is never retried. Any failed request
   starts a cool-down (60 s, doubling to 15 min) during which Guesty is not asked again, and a
   connection that needs attention or is disabled never asks.
3. Every token response's `ratelimit-remaining` and `ratelimit-reset` headers are stored. With one
   token or fewer left the connection is marked `token_budget_low`, and a still-valid token is kept
   instead of being refreshed early. A failed request with nothing left (or a 429) means the budget
   is used up: nothing is retried and `PMSTokenBudgetError` says how long to wait.
4. A 401, or Guesty's 403 "You don't have permission to access", refreshes only if the stored token
   has expired or is inside the refresh window (`refresh_after_rejection`). Otherwise it is a real
   permission problem.

The token is a JWT. Its payload (read without a signature check: it came from Guesty over TLS)
carries `exp`, `cid` and `accountId`. `cid` must match the stored client id, and the account id is
fixed for a connection and unique across connections.
"""

import asyncio
import base64
import binascii
import contextvars
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Final, cast

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pmsbridge.core.crypto import VaultError
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.pms.base import (
    PMSAuthError,
    PMSError,
    PMSTokenBudgetError,
    PMSUpstreamError,
    remaining_seconds,
)
from pmsbridge.pms.db import (
    PMSConnection,
    PMSToken,
    client_id_digest_for,
    client_id_in_use,
    open_credentials,
    open_token,
    seal_token,
    sealed_credential_columns,
)

logger = logging.getLogger(__name__)

TOKEN_URL: Final = "https://open-api.guesty.com/oauth2/token"  # noqa: S105  (an address)
JWT_PARTS: Final = 3
TOKEN_TIMEOUT_SECONDS: Final = 10.0
# The whole token exchange, including reading the body, must end within this time.
TOKEN_TOTAL_SECONDS: Final = 30.0
BUDGET_LOW_AT: Final = 1
# While the budget is low, a token that is still valid is kept until it is this close to expiry.
LOW_BUDGET_SAFETY_SECONDS: Final = 120
SUCCESS_TOUCH_SECONDS: Final = 300.0
MAX_TOKEN_RESPONSE_BYTES: Final = 64 * 1024
COOLDOWN_START_SECONDS: Final = 60
COOLDOWN_MAX_SECONDS: Final = 900
NEEDS_ATTENTION_TEXT: Final = (
    "This connection needs attention: an administrator must check its credentials."
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class GuestyCredentials:
    client_id: str
    client_secret: str = field(repr=False)

    @classmethod
    def from_mapping(cls, values: dict[str, str]) -> "GuestyCredentials":
        return cls(client_id=values["client_id"], client_secret=values["client_secret"])


@dataclass(frozen=True)
class IssuedToken:
    """A token Guesty just issued, with what the response headers said about the budget."""

    access_token: str = field(repr=False)
    expires_at: datetime
    obtained_at: datetime
    account_id: str
    budget_remaining: int | None
    budget_reset_at: datetime | None


class _TokenRequestError(Exception):
    """A failed token request. Carries the client-safe error and what the headers said."""

    def __init__(
        self,
        error: PMSError,
        *,
        remaining: int | None = None,
        reset_at: datetime | None = None,
        rejected: bool = False,
    ) -> None:
        super().__init__("token request failed")
        self.error = error
        self.remaining = remaining
        self.reset_at = reset_at
        self.rejected = rejected


def decode_jwt_claims(token: str) -> dict[str, object] | None:
    """The payload of a JWT, or None when it is not one. The signature is not checked."""
    parts = token.split(".")
    if len(parts) != JWT_PARTS:
        return None
    payload = parts[1]
    try:
        loaded: object = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error):
        return None
    return cast(dict[str, object], loaded) if isinstance(loaded, dict) else None


def _int_header(headers: httpx.Headers, *names: str) -> int | None:
    for name in names:
        value = headers.get(name, "").strip()
        if value.isdecimal():
            return int(value)
    return None


def _budget_message(reset_at: datetime | None, now: datetime) -> str:
    base = "Guesty allows only 5 access tokens per 24 hours and this API key has used them all."
    if reset_at is None:
        return f"{base} Try again later."
    minutes = max(1, math.ceil((reset_at - now).total_seconds() / 60))
    return f"{base} The next one is available in about {minutes} minutes."


async def _request_token(credentials: GuestyCredentials) -> IssuedToken:
    """Ask Guesty for one token. Never retries. Only this module calls it.

    The timeouts (10 seconds per step, 30 seconds in all) are deliberately not shortened to the
    caller's deadline: a token that was requested should be received and stored, because it has
    already been counted. Running out of time takes the same path as a network failure.
    """
    now = _now()
    body: bytes | None
    try:
        async with (
            asyncio.timeout(TOKEN_TOTAL_SECONDS),
            httpx.AsyncClient(
                timeout=httpx.Timeout(TOKEN_TIMEOUT_SECONDS), follow_redirects=False
            ) as client,
            client.stream(
                "POST",
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "client_credentials",
                    "scope": "open-api",
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                },
            ) as response,
        ):
            status, headers = response.status_code, response.headers
            chunks: list[bytes] = []
            size = 0
            body = None
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_TOKEN_RESPONSE_BYTES:
                    break
                chunks.append(chunk)
            else:
                body = b"".join(chunks)
    except (httpx.HTTPError, TimeoutError):
        raise _TokenRequestError(
            PMSUpstreamError(
                "Guesty could not be reached to get an access token. The request was not "
                "repeated, to protect the daily limit of new tokens."
            )
        ) from None

    remaining = _int_header(headers, "ratelimit-remaining", "x-ratelimit-remaining-day")
    reset_seconds = _int_header(headers, "ratelimit-reset")
    reset_at = None if reset_seconds is None else now + timedelta(seconds=reset_seconds)

    if status != HTTPStatus.OK:
        if status == HTTPStatus.TOO_MANY_REQUESTS or remaining == 0:
            raise _TokenRequestError(
                PMSTokenBudgetError(_budget_message(reset_at, now)), remaining=0, reset_at=reset_at
            )
        if status in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise _TokenRequestError(
                PMSAuthError("Guesty did not accept the client ID and secret."),
                remaining=remaining,
                reset_at=reset_at,
                rejected=True,
            )
        raise _TokenRequestError(
            PMSUpstreamError(f"Guesty could not issue an access token (status {status})."),
            remaining=remaining,
            reset_at=reset_at,
        )
    return _read_issued(body, credentials, now, remaining, reset_at)


def _read_issued(
    content: bytes | None,
    credentials: GuestyCredentials,
    now: datetime,
    remaining: int | None,
    reset_at: datetime | None,
) -> IssuedToken:
    unreadable = PMSUpstreamError("Guesty returned an access token that could not be read.")
    try:
        if content is None:
            raise ValueError("response too large")
        body: object = json.loads(content)
    except ValueError:
        raise _TokenRequestError(unreadable, remaining=remaining, reset_at=reset_at) from None
    data = cast(dict[str, object], body) if isinstance(body, dict) else {}
    access_token, expires_in = data.get("access_token"), data.get("expires_in")
    claims = decode_jwt_claims(access_token) if isinstance(access_token, str) else None
    if (
        not isinstance(access_token, str)
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
        or claims is None
        or not isinstance(claims.get("accountId"), str)
    ):
        raise _TokenRequestError(unreadable, remaining=remaining, reset_at=reset_at)
    if claims.get("cid") != credentials.client_id:
        logger.warning("security event: Guesty token was issued for another client id")
        raise _TokenRequestError(
            PMSAuthError("Guesty issued a token for a different API key than the one stored."),
            remaining=remaining,
            reset_at=reset_at,
            rejected=True,
        )
    expires_at = now + timedelta(seconds=expires_in)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        expires_at = min(expires_at, datetime.fromtimestamp(exp, UTC))
    return IssuedToken(
        access_token=access_token,
        expires_at=expires_at,
        obtained_at=now,
        account_id=cast(str, claims["accountId"]),
        budget_remaining=remaining,
        budget_reset_at=reset_at,
    )


async def validate_credentials(client_id: str, client_secret: str) -> IssuedToken:
    """Check a client id and secret by getting one token, which the caller keeps.

    Validation costs one of the five daily tokens, so the result must be stored as the connection's
    token with `store_issued_token`, never thrown away.
    """
    try:
        return await _request_token(GuestyCredentials(client_id, client_secret))
    except _TokenRequestError as failure:
        raise failure.error from None


async def store_issued_token(
    db: AsyncSession, conn: PMSConnection, issued: IssuedToken
) -> PMSError | None:
    """Keep `issued` as the connection's token (inside the caller's write transaction).

    Returns an error instead of storing when the account differs from the one on record or another
    connection already holds the account. Otherwise records the account, the budget and the status.
    """
    error: PMSError | None = None
    if conn.guesty_account_id is not None and conn.guesty_account_id != issued.account_id:
        logger.warning(
            "security event: Guesty account changed for a connection",
            extra={"connection_id": conn.id},
        )
        error = PMSAuthError("These credentials belong to a different Guesty account than before.")
    elif conn.guesty_account_id is None:
        taken = await db.scalar(
            select(PMSConnection.id).where(
                PMSConnection.guesty_account_id == issued.account_id,
                PMSConnection.id != conn.id,
            )
        )
        if taken is not None:
            logger.warning(
                "security event: Guesty account already connected",
                extra={"connection_id": conn.id},
            )
            error = PMSAuthError("This Guesty account is already connected.")
    if error is not None:
        return error
    conn.guesty_account_id = issued.account_id
    row = await db.get(PMSToken, conn.id)
    if row is None:
        row = PMSToken(connection_id=conn.id)
        db.add(row)
    row.access_token = seal_token(conn.id, issued.access_token)
    row.expires_at = issued.expires_at
    row.obtained_at = issued.obtained_at
    _record_budget(conn, row, issued.budget_remaining, issued.budget_reset_at)
    conn.updated_at = conn.last_success_at = issued.obtained_at
    return None


def _record_budget(
    conn: PMSConnection, row: PMSToken | None, remaining: int | None, reset_at: datetime | None
) -> None:
    if row is not None:
        row.budget_remaining, row.budget_reset_at = remaining, reset_at
    if remaining is None or conn.status not in ("active", "token_budget_low"):
        return
    if remaining <= BUDGET_LOW_AT:
        if conn.status != "token_budget_low":
            logger.warning(
                "Guesty token budget is low",
                extra={"connection_id": conn.id, "quota_left": remaining},
            )
        conn.status = "token_budget_low"
    else:
        conn.status = "active"


# One lock per connection, shared by every manager of that connection in this process.
_locks: dict[str, asyncio.Lock] = {}
# The request-and-store task of a connection that is being refreshed right now.
_tasks: dict[str, asyncio.Task[str]] = {}
# When Guesty is known to have no token to give, by connection: the time one frees up.
_blocked_until: dict[str, datetime] = {}
# After a failed token request: (no new request before, length of this pause).
_cooldown: dict[str, tuple[datetime, timedelta]] = {}
_last_touch: dict[str, float] = {}
# A token Guesty issued that could not be written to the database, by connection. It is used and
# stored again before Guesty is asked for another token.
_unsaved: dict[str, IssuedToken] = {}


def forget_connection(connection_id: str) -> None:
    """Forget the blocks, cool-down, unsaved token and last-success time of one connection.

    Called when its credentials are replaced. Locks and refreshes in flight are left alone.
    """
    _blocked_until.pop(connection_id, None)
    _cooldown.pop(connection_id, None)
    _last_touch.pop(connection_id, None)
    _unsaved.pop(connection_id, None)


def reset_token_state() -> None:
    """Forget all in-memory state. For tests only: it also drops locks and refreshes in flight."""
    _locks.clear()
    _tasks.clear()
    _blocked_until.clear()
    _cooldown.clear()
    _last_touch.clear()
    _unsaved.clear()


SUPERSEDED_TEXT: Final = (
    "The credentials of this connection were replaced while a token was being requested. "
    "The old token was discarded. Try again."
)


def _issued_for_current_key(issued: IssuedToken, conn: PMSConnection) -> bool:
    """Compare and swap: only a token for the client id now stored may be stored.

    A token requested with credentials that were replaced meanwhile (here, or by a command run
    in another process) is discarded instead of being written over the new key's token.
    """
    claims = decode_jwt_claims(issued.access_token)
    cid = claims.get("cid") if claims else None
    return isinstance(cid, str) and client_id_digest_for(cid) == conn.client_id_digest


def _check_usable(conn: PMSConnection | None) -> PMSConnection:
    if conn is None or conn.status == "disabled":
        raise PMSAuthError("This connection is not available.")
    if conn.status == "needs_attention":
        raise PMSAuthError(NEEDS_ATTENTION_TEXT)
    return conn


async def replace_credentials(connection_id: str, client_id: str, client_secret: str) -> None:
    """Swap an existing connection's credentials for new ones that Guesty accepts.

    The only way to test credentials for a connection that already exists. It takes the connection
    lock and waits for a refresh in flight, so no other token request runs meanwhile. Under the
    lock it does nothing (and asks for no token) when the stored credentials already equal the
    submitted ones and the stored token is usable, and it refuses a key that another connection
    uses. Otherwise it gets one token with the new credentials (one of the five daily tokens), and
    in a single write replaces the credentials and token, keeps the connection id, and puts a
    connection that needed attention back to active. The old credentials stay untouched unless the
    new ones are accepted. Raises a `PMSError` otherwise (including a different account).
    """
    async with _locks.setdefault(connection_id, asyncio.Lock()):
        task = _tasks.get(connection_id)
        if task is not None and not task.done():
            await asyncio.wait({task})
        if await _already_current(connection_id, client_id, client_secret):
            return
        issued = await validate_credentials(client_id, client_secret)
        error: PMSError | None = None
        for attempt in (1, 2):  # the token is already counted: one more try to write it
            try:
                error = await _swap(connection_id, client_id, client_secret, issued)
            except IntegrityError:
                error = PMSAuthError("This API key or Guesty account is already connected.")
            except SQLAlchemyError as failure:
                logger.error(
                    "replacement credentials could not be stored: %s",
                    type(failure).__name__,
                    extra={"connection_id": connection_id},
                )
                if attempt == 1:
                    continue
                raise PMSUpstreamError(
                    "Guesty accepted the key but it could not be saved; one daily token was used."
                ) from None
            break
        if error is not None:
            raise error
        forget_connection(connection_id)


async def _already_current(connection_id: str, client_id: str, client_secret: str) -> bool:
    """True when nothing needs replacing: same credentials stored and a usable token held."""
    async with session() as db:
        conn = await db.get(PMSConnection, connection_id)
        if conn is None:
            raise PMSAuthError("This connection is not available.")
        if await client_id_in_use(db, client_id, except_connection_id=connection_id):
            raise PMSAuthError("This API key is already used by another connection.")
        row = await db.get(PMSToken, connection_id)
    try:
        stored = open_credentials(conn)
    except (VaultError, ValueError):
        return False
    same = stored.get("client_id") == client_id and stored.get("client_secret") == client_secret
    usable = (
        row is not None
        and conn.status in ("active", "token_budget_low")
        and row.expires_at - _now() > timedelta(seconds=LOW_BUDGET_SAFETY_SECONDS)
    )
    return same and usable


async def _swap(
    connection_id: str, client_id: str, client_secret: str, issued: IssuedToken
) -> PMSError | None:
    async with write_transaction() as db:
        conn = await db.get(PMSConnection, connection_id)
        if conn is None:
            raise PMSAuthError("This connection is not available.")
        if await client_id_in_use(db, client_id, except_connection_id=connection_id):
            return PMSAuthError("This API key is already used by another connection.")
        error = await store_issued_token(db, conn, issued)
        if error is not None:
            return error
        credentials = {"client_id": client_id, "client_secret": client_secret}
        for column, value in sealed_credential_columns(connection_id, credentials).items():
            setattr(conn, column, value)
        if conn.status == "needs_attention":
            conn.status = "active"
            _record_budget(conn, None, issued.budget_remaining, issued.budget_reset_at)
    return None


class GuestyTokenManager:
    def __init__(
        self, connection_id: str, credentials: GuestyCredentials, account_id: str | None = None
    ) -> None:
        self.connection_id = connection_id
        self._credentials = credentials
        self.account_id = account_id

    @staticmethod
    def _window() -> timedelta:
        return timedelta(minutes=get_settings().guesty_token_refresh_minutes)

    @staticmethod
    def _budget_low(row: PMSToken, now: datetime) -> bool:
        return (
            row.budget_remaining is not None
            and row.budget_remaining <= BUDGET_LOW_AT
            and (row.budget_reset_at is None or now < row.budget_reset_at)
        )

    def _reusable(self, row: PMSToken, now: datetime) -> bool:
        """True when the stored token needs no refresh yet."""
        left = row.expires_at - now
        if left > self._window():
            return True
        # With the budget low, do not refresh early: keep a valid token until it nearly expires.
        return left > timedelta(seconds=LOW_BUDGET_SAFETY_SECONDS) and self._budget_low(row, now)

    def _stale(self, row: PMSToken, token: str, now: datetime) -> bool:
        """True when the token has expired or is inside the refresh window."""
        expires_at = row.expires_at
        claims = decode_jwt_claims(token)
        exp = claims.get("exp") if claims else None
        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            expires_at = min(expires_at, datetime.fromtimestamp(exp, UTC))
        return expires_at - now <= self._window()

    async def get_token(self) -> str:
        """A usable access token. Asks Guesty for a new one only when the stored one is due."""
        async with session() as db:
            row = await db.get(PMSToken, self.connection_id)
            if row is not None and self._reusable(row, _now()):
                return open_token(row)
        token = await self._refresh(rejected=None)
        if token is None:  # only for a rejected token; cannot happen here
            raise PMSAuthError("No access token is available for this connection.")
        return token

    async def refresh_after_rejection(self, rejected: str) -> str | None:
        """After a 401 or Guesty's 403 for `rejected`: a token to retry with, or None.

        None means the token is still comfortably valid, so the rejection is a real permission
        problem and nothing was requested. A token already refreshed by someone else is reused.
        """
        return await self._refresh(rejected=rejected)

    async def mark_needs_attention(self) -> None:
        async with write_transaction() as db:
            conn = await db.get(PMSConnection, self.connection_id)
            if conn is not None and conn.status != "disabled":
                conn.status, conn.updated_at = "needs_attention", _now()

    async def touch_success(self) -> None:
        """Record a successful call (at most every few minutes, to avoid a write per request)."""
        loop_time = asyncio.get_running_loop().time()
        if loop_time - _last_touch.get(self.connection_id, -SUCCESS_TOUCH_SECONDS) < (
            SUCCESS_TOUCH_SECONDS
        ):
            return
        _last_touch[self.connection_id] = loop_time
        async with write_transaction() as db:
            conn = await db.get(PMSConnection, self.connection_id)
            if conn is not None:
                conn.last_success_at = _now()

    async def _refresh(self, rejected: str | None) -> str | None:
        """One refresh at a time per connection; the caller's deadline bounds only its waiting."""
        cid = self.connection_id
        lock = _locks.setdefault(cid, asyncio.Lock())
        limit = asyncio.timeout(remaining_seconds())
        try:
            async with limit, lock:
                task = _tasks.get(cid)
                if task is None or task.done():
                    token, must_ask = await self._read_state(rejected)
                    if not must_ask:
                        return token
                    # An empty context: the caller's deadline must not apply inside the task.
                    task = asyncio.create_task(
                        self._request_and_store(), context=contextvars.Context()
                    )
                    _tasks[cid] = task
                    task.add_done_callback(self._finished)
                # Shielded: if this caller is cancelled or times out, the task still finishes and
                # stores the token Guesty issued, and the next caller waits for it.
                return await asyncio.shield(task)
        except TimeoutError:
            if not limit.expired():
                raise PMSUpstreamError(
                    "The Guesty access token could not be renewed. Try again shortly."
                ) from None
            raise PMSUpstreamError(
                "The Guesty access token is still being renewed. Try again shortly."
            ) from None

    def _finished(self, task: asyncio.Task[str]) -> None:
        if _tasks.get(self.connection_id) is task:
            del _tasks[self.connection_id]
        if not task.cancelled():
            task.exception()  # mark as retrieved: callers that gave up never read it

    async def _read_state(self, rejected: str | None) -> tuple[str | None, bool]:
        """(token, False) when no request is needed, (None, True) when Guesty must be asked.

        Raises when Guesty must not be asked: the connection is unusable or a block is active.
        """
        async with session() as db:
            conn = await db.get(PMSConnection, self.connection_id)
        _check_usable(conn)
        pending = await self._store_unsaved()
        async with session() as db:
            row = await db.get(PMSToken, self.connection_id)
        now = _now()
        current = open_token(row) if row is not None else None
        if pending is not None:
            # Still not storable: use the token Guesty issued, as if it had been stored.
            row = PMSToken(
                connection_id=self.connection_id,
                access_token=b"",
                expires_at=pending.expires_at,
                obtained_at=pending.obtained_at,
                budget_remaining=pending.budget_remaining,
                budget_reset_at=pending.budget_reset_at,
            )
            current = pending.access_token
        if row is not None and current is not None:
            if rejected is not None:
                if current != rejected:
                    return current, False  # another caller already refreshed
                if not self._stale(row, current, now):
                    return None, False
            elif self._reusable(row, now):
                return current, False
        block = self._block(row, now)
        if block is not None:
            if (
                rejected is None
                and row is not None
                and current is not None
                and row.expires_at - now > timedelta(seconds=LOW_BUDGET_SAFETY_SECONDS)
            ):
                return current, False  # not allowed to ask, but this token still works
            raise block
        return None, True

    def _block(self, row: PMSToken | None, now: datetime) -> PMSError | None:
        """The error to raise instead of asking Guesty, or None when asking is allowed."""
        until = _blocked_until.get(self.connection_id)
        if until is None and row is not None and row.budget_remaining == 0:
            until = row.budget_reset_at
        if until is not None and now < until:
            return PMSTokenBudgetError(_budget_message(until, now))
        pause = _cooldown.get(self.connection_id)
        if pause is not None and now < pause[0]:
            seconds = max(1, math.ceil((pause[0] - now).total_seconds()))
            return PMSUpstreamError(
                "Guesty did not issue an access token a moment ago, so new attempts are paused "
                f"to protect the daily token limit. Try again in about {seconds} seconds."
            )
        return None

    async def _request_and_store(self) -> str:
        """Ask Guesty for a token and store it. Runs as a task that no caller can cancel."""
        cid = self.connection_id
        try:
            issued = await _request_token(self._credentials)
        except _TokenRequestError as exc:
            await self._record_failure(exc)
            raise exc.error from None
        self._note_budget(issued.budget_remaining, issued.budget_reset_at, issued.obtained_at)
        try:
            await self._store(issued)
        except SQLAlchemyError as failure:
            # Guesty already counted this token. Keep it in memory and store it on the next call.
            logger.error(
                "a Guesty token could not be stored: %s",
                type(failure).__name__,
                extra={"connection_id": cid},
            )
            _unsaved[cid] = issued
            self._start_cooldown(_now())
            raise PMSUpstreamError(
                "Guesty issued an access token but it could not be saved. It will be used and "
                "saved on the next request. Try again shortly."
            ) from None
        return issued.access_token

    async def _store(self, issued: IssuedToken) -> None:
        """Store a token in a short write. Raises a `PMSError` for a refused token, and lets other
        database errors through so the caller can keep the token."""
        cid = self.connection_id
        error: PMSError | None = None
        try:
            async with write_transaction() as db:
                conn = _check_usable(await db.get(PMSConnection, cid))
                if not _issued_for_current_key(issued, conn):
                    raise PMSUpstreamError(SUPERSEDED_TEXT)
                error = await store_issued_token(db, conn, issued)
                if error is not None:
                    conn.status, conn.updated_at = "needs_attention", _now()
        except IntegrityError:
            error = PMSAuthError("This Guesty account is already connected.")
            await self.mark_needs_attention()
        if error is not None:
            raise error
        _cooldown.pop(cid, None)
        self.account_id = issued.account_id

    async def _store_unsaved(self) -> IssuedToken | None:
        """Try again to store a token that could not be saved. Returns it while it still is not."""
        cid = self.connection_id
        pending = _unsaved.get(cid)
        if pending is None:
            return None
        if pending.expires_at - _now() <= timedelta(seconds=LOW_BUDGET_SAFETY_SECONDS):
            del _unsaved[cid]  # too close to its end to be worth keeping
            return None
        try:
            await self._store(pending)
        except SQLAlchemyError:
            return pending
        except PMSError as refused:
            del _unsaved[cid]
            if refused.message == SUPERSEDED_TEXT:
                return None  # a token for the old key: dropped, the caller carries on
            raise
        del _unsaved[cid]
        return None

    def _start_cooldown(self, now: datetime) -> None:
        """Pause token requests: 60 seconds, doubling after each failure up to 15 minutes."""
        previous = _cooldown.get(self.connection_id)
        delay = (
            timedelta(seconds=COOLDOWN_START_SECONDS)
            if previous is None
            else min(previous[1] * 2, timedelta(seconds=COOLDOWN_MAX_SECONDS))
        )
        _cooldown[self.connection_id] = (now + delay, delay)

    def _note_budget(self, remaining: int | None, reset_at: datetime | None, now: datetime) -> None:
        """Remember in memory when the budget is empty, even if there is no token row to hold it."""
        if remaining == 0:
            _blocked_until[self.connection_id] = reset_at or now + timedelta(minutes=15)
        elif remaining is not None:
            _blocked_until.pop(self.connection_id, None)

    async def _record_failure(self, exc: _TokenRequestError) -> None:
        now = _now()
        cid = self.connection_id
        self._note_budget(exc.remaining, exc.reset_at, now)
        if not isinstance(exc.error, PMSTokenBudgetError):
            self._start_cooldown(now)
        async with write_transaction() as db:
            conn = await db.get(PMSConnection, cid)
            if conn is None:
                return
            row = await db.get(PMSToken, cid)
            conn.updated_at = now
            if exc.rejected:
                conn.status = "needs_attention" if conn.status != "disabled" else conn.status
            elif exc.remaining is not None:
                _record_budget(conn, row, exc.remaining, exc.reset_at)
