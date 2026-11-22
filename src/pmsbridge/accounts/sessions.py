"""Server-side sessions.

The browser holds a random 32-byte value. The database holds only its SHA-256, so a copy of the
database cannot be used to sign in. A session ends after `SESSION_IDLE_HOURS` without use or
`SESSION_MAX_DAYS` after it was created, whichever comes first, and when the user is disabled.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, or_, select, update

from pmsbridge.accounts import csrf
from pmsbridge.accounts.db import Session, User
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import session, write_transaction

TOUCH_INTERVAL: Final = timedelta(seconds=60)
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    user_id: int
    email: str
    role: str
    csrf_secret: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _idle_limit() -> timedelta:
    return timedelta(hours=get_settings().session_idle_hours)


def _max_age() -> timedelta:
    return timedelta(days=get_settings().session_max_days)


async def create_session(user_id: int, user_agent: str | None = None) -> str:
    """Start a session and return the cookie value. Expired sessions are cleaned up on the way."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    agent = hashlib.sha256(user_agent[:512].encode()).hexdigest() if user_agent else None
    async with write_transaction() as db:
        await db.execute(
            delete(Session).where(
                or_(
                    Session.expires_at <= now,
                    Session.created_at <= now - _max_age(),
                    Session.last_seen_at <= now - _idle_limit(),
                )
            )
        )
        db.add(
            Session(
                id=hash_token(token),
                user_id=user_id,
                csrf_secret=csrf.new_secret(),
                created_at=now,
                last_seen_at=now,
                expires_at=now + _max_age(),
                user_agent_hash=agent,
            )
        )
    return token


async def load_session(token: str | None) -> SessionInfo | None:
    """The session for a cookie value, or None if unknown, expired, or its user is disabled."""
    if token is None or not _TOKEN_SHAPE.fullmatch(token):
        return None
    session_id = hash_token(token)
    async with session() as db:
        row = (
            await db.execute(
                select(Session, User)
                .join(User, User.id == Session.user_id)
                .where(Session.id == session_id)
            )
        ).first()
    if row is None:
        return None
    stored, user = row
    now = datetime.now(UTC)
    expired = (
        stored.expires_at <= now
        or stored.created_at <= now - _max_age()
        or stored.last_seen_at <= now - _idle_limit()
    )
    if expired or not user.is_active:
        await end_session(token)
        return None
    if now - stored.last_seen_at >= TOUCH_INTERVAL:
        async with write_transaction() as db:
            await db.execute(
                update(Session).where(Session.id == session_id).values(last_seen_at=now)
            )
    return SessionInfo(session_id, user.id, user.email, user.role, stored.csrf_secret)


async def end_session(token: str) -> None:
    async with write_transaction() as db:
        await db.execute(delete(Session).where(Session.id == hash_token(token)))


async def end_user_sessions(user_id: int) -> int:
    """Sign a user out everywhere (disable, password reset). Returns how many sessions ended."""
    async with write_transaction() as db:
        result = await db.execute(delete(Session).where(Session.user_id == user_id))
    return int(result.rowcount)  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownArgumentType]
