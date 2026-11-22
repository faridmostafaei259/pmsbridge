"""Grants and authorization codes: creating them at consent, using a code up, revoking grants.

A code is 32 random bytes, stored only as a SHA-256, valid for 5 minutes, and usable once. Using a
code a second time revokes the grant it belonged to (OAuth 2.1: a replayed code means the code was
probably stolen). A revoked grant issues nothing, and grants are revoked when their person is
disabled, demoted or promoted, or has their sign-in or password reset.
"""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pmsbridge.accounts.sessions import hash_token
from pmsbridge.core.storage import write_transaction
from pmsbridge.oauth.db import OAuthClient, OAuthCode, OAuthGrant

CODE_LIFETIME: Final = timedelta(minutes=5)
_VERIFIER_SHAPE = re.compile(r"[A-Za-z0-9._~-]{43,128}")


@dataclass(frozen=True)
class Selection:
    """Which PMS connections the person allowed."""

    all_connections: bool
    connection_ids: list[str]


@dataclass(frozen=True)
class CodeRequest:
    """What the code and grant remember about the authorization request."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str


@dataclass(frozen=True)
class RedeemedCode:
    """A code that was valid and is now used up. The token endpoint issues tokens for it."""

    grant_id: int
    client_id: str
    user_id: int
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str


class ClientGoneError(Exception):
    """The client expired or was removed while the person was deciding."""


async def create_grant_and_code(user_id: int, request: CodeRequest, selection: Selection) -> str:
    """Record the person's consent and return the authorization code (shown only once)."""
    code = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    async with write_transaction() as db:
        client = await db.get(OAuthClient, request.client_id)
        if client is None:
            raise ClientGoneError
        client.last_used_at = now
        await db.execute(delete(OAuthCode).where(OAuthCode.expires_at <= now))
        grant = OAuthGrant(
            user_id=user_id,
            client_id=request.client_id,
            all_connections=selection.all_connections,
            allowed_connection_ids=selection.connection_ids,
            scope=request.scope,
            resource=request.resource,
            created_at=now,
        )
        db.add(grant)
        await db.flush()
        db.add(
            OAuthCode(
                code_hash=hash_token(code),
                grant_id=grant.id,
                client_id=request.client_id,
                user_id=user_id,
                redirect_uri=request.redirect_uri,
                code_challenge=request.code_challenge,
                resource=request.resource,
                scope=request.scope,
                created_at=now,
                expires_at=now + CODE_LIFETIME,
            )
        )
    return code


def _pkce_matches(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256: BASE64URL(SHA-256(verifier)) without padding equals the challenge."""
    if not _VERIFIER_SHAPE.fullmatch(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return secrets.compare_digest(computed, challenge)


async def redeem_code(
    code: str, client_id: str, redirect_uri: str, code_verifier: str
) -> RedeemedCode | None:
    """Use a code up. None if it cannot be used; the code is spent only when everything matches.

    Checked inside one transaction, before the code is marked used: the code exists, has not
    expired, belongs to `client_id` and `redirect_uri` (exact match), its grant is not revoked, and
    the verifier hashes (S256) to the stored challenge. A wrong client, address or verifier returns
    None and leaves the code usable by its real owner. Presenting a code that was already used
    revokes its grant (OAuth 2.1: a replayed code means the code was probably stolen).
    """
    now = datetime.now(UTC)
    async with write_transaction() as db:
        row = await db.get(OAuthCode, hash_token(code))
        if row is None or row.expires_at <= now:
            return None
        grant = await db.get(OAuthGrant, row.grant_id)
        if row.used_at is not None:
            if grant is not None and grant.revoked_at is None:
                grant.revoked_at = now
            return None
        if grant is None or grant.revoked_at is not None:
            return None
        if (
            row.client_id != client_id
            or row.redirect_uri != redirect_uri
            or not _pkce_matches(code_verifier, row.code_challenge)
        ):
            return None
        row.used_at = now
        return RedeemedCode(
            grant_id=row.grant_id,
            client_id=row.client_id,
            user_id=row.user_id,
            redirect_uri=row.redirect_uri,
            code_challenge=row.code_challenge,
            resource=row.resource,
            scope=row.scope,
        )


async def revoke_user_grants_in(db: AsyncSession, user_id: int) -> None:
    """Revoke every grant of a person, inside the caller's transaction.

    Called when the person is disabled, demoted or promoted, or their two-factor sign-in or
    password is reset. Tokens need no separate step: they are valid only while their grant is.
    """
    await db.execute(
        update(OAuthGrant)
        .where(OAuthGrant.user_id == user_id, OAuthGrant.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


@dataclass(frozen=True)
class RevokeSummary:
    """How much a bulk revocation ended: approvals, the people who gave them, the assistants."""

    grants: int
    people: int
    assistants: int


async def revoke_grants(
    *, user_id: int | None = None, client_id: str | None = None
) -> RevokeSummary:
    """Revoke every live grant that matches. With neither filter, every live grant.

    Returns the counts. Used by the connected-apps pages and the `oauth revoke-ai-access` command.
    """
    now = datetime.now(UTC)
    async with write_transaction() as db:
        query = select(OAuthGrant).where(OAuthGrant.revoked_at.is_(None))
        if user_id is not None:
            query = query.where(OAuthGrant.user_id == user_id)
        if client_id is not None:
            query = query.where(OAuthGrant.client_id == client_id)
        live = list((await db.execute(query)).scalars())
        for grant in live:
            grant.revoked_at = now
        return RevokeSummary(
            len(live),
            len({grant.user_id for grant in live}),
            len({grant.client_id for grant in live}),
        )
