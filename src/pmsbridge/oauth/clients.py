"""Registered clients: lookup, creation and expiry.

A registration that is never used for an authorization ends after 24 hours; one that was used ends
after 90 days without activity (a person authorizing it again, and later a token being issued or
refreshed, count as activity). Deleting a client deletes its grants and codes.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import and_, delete, or_

from pmsbridge.accounts.sessions import hash_token
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth.db import OAuthClient

UNUSED_LIFETIME: Final = timedelta(hours=24)
IDLE_LIFETIME: Final = timedelta(days=90)
_CLIENT_ID_SHAPE = re.compile(r"[A-Za-z0-9_-]{16,64}")


@dataclass(frozen=True)
class RegistrationData:
    """Validated registration metadata."""

    name: str
    redirect_uris: list[str]
    auth_method: str
    grant_types: list[str]


@dataclass(frozen=True)
class NewClient:
    client_id: str
    client_secret: str | None
    created_at: datetime


def _expired_clause(now: datetime) -> object:
    return or_(
        and_(OAuthClient.last_used_at.is_(None), OAuthClient.created_at <= now - UNUSED_LIFETIME),
        OAuthClient.last_used_at <= now - IDLE_LIFETIME,
    )


def is_expired(client: OAuthClient, now: datetime) -> bool:
    if client.last_used_at is None:
        return client.created_at <= now - UNUSED_LIFETIME
    return client.last_used_at <= now - IDLE_LIFETIME


async def get_client(client_id: str) -> OAuthClient | None:
    """The live client with this id, or None (unknown, malformed or expired)."""
    if not _CLIENT_ID_SHAPE.fullmatch(client_id):
        return None
    async with session() as db:
        client = await db.get(OAuthClient, client_id)
    if client is None or is_expired(client, datetime.now(UTC)):
        return None
    return client


async def create_client(data: RegistrationData) -> NewClient:
    """Store a client and drop expired ones. A secret is returned once and kept only as a hash."""
    client_id = secrets.token_urlsafe(24)
    secret = None if data.auth_method == "none" else secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    async with write_transaction() as db:
        await db.execute(delete(OAuthClient).where(_expired_clause(now)))  # pyright: ignore[reportArgumentType]
        db.add(
            OAuthClient(
                client_id=client_id,
                client_secret_hash=None if secret is None else hash_token(secret),
                client_name=data.name,
                redirect_uris=data.redirect_uris,
                token_endpoint_auth_method=data.auth_method,
                grant_types=data.grant_types,
                created_at=now,
            )
        )
    return NewClient(client_id, secret, now)
