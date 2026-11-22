"""Access and refresh tokens: issuing, rotating, checking and revoking them (ADR 0011).

Tokens are 32 random bytes, stored only as a SHA-256. An access token lasts one hour. A refresh
token is used once: every refresh returns a new refresh token and marks the old one as replaced. A
refresh token ends 30 days after it was issued and never later than 90 days after the person made
the grant. Presenting a replaced or revoked refresh token again means a copy was probably stolen, so
the whole grant (and with it every token) is revoked. A grant can be refreshed at most 10 times in a
minute: a client in a runaway loop is told to wait (the refresh token is not used up).

A token is valid only while its grant is, so revoking a grant ends all its tokens at once. That is
how disabling a person, resetting their password or two-factor sign-in, or changing their role
ends assistant access immediately: those actions revoke the person's grants.

`validate_access_token` is what the MCP endpoint calls on every request.
"""

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmsbridge.accounts.db import User, UserTotp
from pmsbridge.accounts.sessions import hash_token
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import clients
from pmsbridge.oauth.config import ACCESS_TOKEN_SECONDS, mcp_resource
from pmsbridge.oauth.db import OAuthClient, OAuthGrant, OAuthToken
from pmsbridge.oauth.grants import RedeemedCode

logger = logging.getLogger("pmsbridge.oauth")

ACCESS_LIFETIME: Final = timedelta(seconds=ACCESS_TOKEN_SECONDS)
REFRESH_IDLE_LIFETIME: Final = timedelta(days=30)
REFRESH_MAX_LIFETIME: Final = timedelta(days=90)
REFRESHES_PER_MINUTE: Final = 10
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43,128}")


@dataclass(frozen=True)
class IssuedTokens:
    """What the token endpoint returns. The values are shown once and are not stored."""

    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str


@dataclass(frozen=True)
class RefreshLimited:
    """The grant was refreshed too often; the client may try again after `retry_after` seconds."""

    retry_after: int


@dataclass(frozen=True)
class AccessPrincipal:
    """Who an access token speaks for, as checked just now."""

    user_id: int
    client_id: str
    grant_id: int
    scope: str
    all_connections: bool
    allowed_connection_ids: tuple[str, ...]
    expires_at: datetime


def _add_token(
    out: list[OAuthToken], kind: str, grant: OAuthGrant, now: datetime, expires_at: datetime
) -> str:
    value = secrets.token_urlsafe(32)
    out.append(
        OAuthToken(
            token_hash=hash_token(value),
            kind=kind,
            grant_id=grant.id,
            client_id=grant.client_id,
            user_id=grant.user_id,
            resource=grant.resource,
            scope=grant.scope,
            created_at=now,
            expires_at=expires_at,
        )
    )
    return value


def _refresh_expiry(grant: OAuthGrant, now: datetime) -> datetime:
    return min(now + REFRESH_IDLE_LIFETIME, grant.created_at + REFRESH_MAX_LIFETIME)


async def issue_for_code(redeemed: RedeemedCode, *, with_refresh: bool) -> IssuedTokens | None:
    """Tokens for a code that was just redeemed. None if the grant was revoked in the meantime."""
    now = datetime.now(UTC)
    new: list[OAuthToken] = []
    async with write_transaction() as db:
        grant = await db.get(OAuthGrant, redeemed.grant_id)
        client = await db.get(OAuthClient, redeemed.client_id)
        if grant is None or grant.revoked_at is not None or client is None:
            return None
        client.last_used_at = now
        grant.last_used_at = now
        await db.execute(delete(OAuthToken).where(OAuthToken.expires_at <= now))
        access = _add_token(new, "access", grant, now, now + ACCESS_LIFETIME)
        refresh = (
            _add_token(new, "refresh", grant, now, _refresh_expiry(grant, now))
            if with_refresh
            else None
        )
        db.add_all(new)
        return IssuedTokens(access, refresh, ACCESS_TOKEN_SECONDS, grant.scope)


async def _find_refresh(
    db: AsyncSession, token: str, client_id: str
) -> tuple[OAuthToken, OAuthGrant] | None:
    """The refresh token row and its grant, if the token is this client's."""
    row = await db.get(OAuthToken, hash_token(token))
    if row is None or row.kind != "refresh" or row.client_id != client_id:
        return None
    grant = await db.get(OAuthGrant, row.grant_id)
    return None if grant is None else (row, grant)


async def _may_refresh(db: AsyncSession, row: OAuthToken, grant: OAuthGrant, now: datetime) -> bool:
    """Whether the refresh token may be exchanged now. An administrator without two-factor
    sign-in is refused and the grant is revoked.
    """
    user = await db.get(User, grant.user_id)
    if (
        row.expires_at <= now
        or grant.revoked_at is not None
        or now >= grant.created_at + REFRESH_MAX_LIFETIME
        or user is None
        or not user.is_active
        or user.setup_pending
    ):
        return False
    if user.role == "admin" and await db.get(UserTotp, user.id) is None:
        grant.revoked_at = now
        return False
    return True


async def _refresh_wait(db: AsyncSession, grant: OAuthGrant, now: datetime) -> int:
    """Seconds to wait if the grant was refreshed 10 times in the last minute, otherwise 0.

    A refresh is counted when it retires a refresh token (`replaced_by` is set), so the refresh
    token that came with the approval does not count.
    """
    recent = list(
        (
            await db.execute(
                select(OAuthToken.revoked_at)
                .where(
                    OAuthToken.grant_id == grant.id,
                    OAuthToken.replaced_by.is_not(None),
                    OAuthToken.revoked_at > now - timedelta(minutes=1),
                )
                .order_by(OAuthToken.revoked_at.desc())
                .limit(REFRESHES_PER_MINUTE)
            )
        ).scalars()
    )
    if len(recent) < REFRESHES_PER_MINUTE or recent[-1] is None:
        return 0
    wait = recent[-1] + timedelta(minutes=1) - now
    return max(1, int(wait.total_seconds()) + 1)


async def rotate_refresh_token(token: str, client_id: str) -> IssuedTokens | RefreshLimited | None:
    """Exchange a refresh token for new tokens. None if it cannot be used.

    Everything is checked and changed in one transaction, so two requests with the same token
    cannot both succeed: the second finds the token already replaced, which counts as reuse and
    revokes the grant. A token that belongs to another client, or has expired, is refused without
    touching the grant. An administrator whose two-factor sign-in is gone gets nothing and the
    grant is revoked. A grant refreshed 10 times in a minute gets `RefreshLimited` and the token
    stays usable.
    """
    if not _TOKEN_SHAPE.fullmatch(token):
        return None
    now = datetime.now(UTC)
    new: list[OAuthToken] = []
    async with write_transaction() as db:
        found = await _find_refresh(db, token, client_id)
        if found is None:
            return None
        row, grant = found
        if row.replaced_by is not None or row.revoked_at is not None:
            if grant.revoked_at is None:
                grant.revoked_at = now
                logger.warning("refresh token reused; grant revoked", extra={"grant_id": grant.id})
            return None
        client = await db.get(OAuthClient, client_id)
        if client is None or not await _may_refresh(db, row, grant, now):
            return None
        if wait := await _refresh_wait(db, grant, now):
            return RefreshLimited(wait)
        client.last_used_at = now
        grant.last_used_at = now
        await db.execute(delete(OAuthToken).where(OAuthToken.expires_at <= now))
        access = _add_token(new, "access", grant, now, now + ACCESS_LIFETIME)
        refresh = _add_token(new, "refresh", grant, now, _refresh_expiry(grant, now))
        row.revoked_at = now
        row.replaced_by = new[1].token_hash
        db.add_all(new)
        return IssuedTokens(access, refresh, ACCESS_TOKEN_SECONDS, grant.scope)


async def revoke_token(token: str, client_id: str) -> None:
    """Revoke a token the client holds (RFC 7009). Unknown tokens and other clients' tokens are
    ignored without any difference the caller could see. Revoking a refresh token revokes the
    grant, because the client is saying the approval is over.
    """
    if not _TOKEN_SHAPE.fullmatch(token):
        return
    now = datetime.now(UTC)
    async with write_transaction() as db:
        row = await db.get(OAuthToken, hash_token(token))
        if row is None or row.client_id != client_id:
            return
        if row.revoked_at is None:
            row.revoked_at = now
        if row.kind == "refresh":
            grant = await db.get(OAuthGrant, row.grant_id)
            if grant is not None and grant.revoked_at is None:
                grant.revoked_at = now


async def validate_access_token(token: str) -> AccessPrincipal | None:
    """Who the access token speaks for, or None for any reason it cannot be used.

    The token is looked up by its hash (one indexed read), then its grant, assistant and person
    are read in a few more lookups on the same connection. Checked: the token exists, is an
    access token, has not expired or been revoked, and is bound to this server's MCP address; its
    grant is not revoked; the assistant is still registered and not expired; and the person is
    active and has finished setup (an administrator must also still have two-factor sign-in on).
    Nothing is written and the token is never logged; the caller learns nothing about why a
    token failed.
    """
    if not _TOKEN_SHAPE.fullmatch(token):
        return None
    now = datetime.now(UTC)
    async with session() as db:
        row = await db.get(OAuthToken, hash_token(token))
        if (
            row is None
            or row.kind != "access"
            or row.revoked_at is not None
            or row.expires_at <= now
            or row.resource != mcp_resource()
        ):
            return None
        grant = await db.get(OAuthGrant, row.grant_id)
        client = await db.get(OAuthClient, row.client_id)
        user = await db.get(User, row.user_id)
        if (
            grant is None
            or grant.revoked_at is not None
            or client is None
            or clients.is_expired(client, now)
            or user is None
            or not user.is_active
            or user.setup_pending
            or grant.user_id != user.id
        ):
            return None
        if user.role == "admin" and await db.get(UserTotp, user.id) is None:
            return None
        return AccessPrincipal(
            user_id=user.id,
            client_id=client.client_id,
            grant_id=grant.id,
            scope=row.scope,
            all_connections=grant.all_connections,
            allowed_connection_ids=tuple(str(item) for item in grant.allowed_connection_ids),
            expires_at=row.expires_at,
        )
