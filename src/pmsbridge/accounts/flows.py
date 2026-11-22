"""One-time links and the short-lived page flows that follow them.

A setup, invite or password-reset link carries 32 random bytes in its query string. Only the
SHA-256 is stored, a link works once and ends after 24 hours (72 for an invite), and a new link
for the same person and purpose replaces the old one. Opening the link does not use it up. The
page that receives it moves the person into a server-side *flow* (a row found by a `__Host-`
cookie) and redirects to a page with no token in its address, so the token does not stay in the
browser history. The last form of the flow uses the link up.

Flows also carry the half-finished states of sign-in: password accepted but two-factor code still
needed, and two-factor enrollment.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pmsbridge.accounts import csrf
from pmsbridge.accounts.auth import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    PageError,
    clear_cookie,
    set_cookie,
)
from pmsbridge.accounts.db import AuthFlow, OneTimeToken, User
from pmsbridge.accounts.reauth import forget_confirmation
from pmsbridge.accounts.sessions import create_session, end_session, hash_token
from pmsbridge.core.storage import session, write_transaction

FLOW_COOKIE: Final = "__Host-pmsbridge_flow"
TOKEN_LIFETIME: Final = timedelta(hours=24)
LINK_LIFETIMES: Final = {
    "setup": TOKEN_LIFETIME,
    "reset": TOKEN_LIFETIME,
    "invite": timedelta(hours=72),
}
FLOW_LIFETIMES: Final = {
    "setup": timedelta(minutes=30),
    "invite": timedelta(minutes=30),
    "reset": timedelta(minutes=30),
    "second_factor": timedelta(minutes=5),
    "enroll": timedelta(minutes=15),
    "enable": timedelta(minutes=15),
}
LINK_KINDS: Final = ("setup", "reset", "invite")
# Links for an account that is not active yet (the administrator created from the command line,
# and an invited person): the same pages, the same rules.
SETUP_KINDS: Final = ("setup", "invite")
_VALUE_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
EXPIRED = "This page has expired. Start again from the sign-in page."


@dataclass(frozen=True)
class Flow:
    flow: AuthFlow
    user: User


def rows_changed(result: object) -> int:
    """How many rows an UPDATE or DELETE touched."""
    return int(getattr(result, "rowcount", 0))


def link_for(path: str, token: str, base_url: str) -> str:
    return f"{base_url}{path}?token={token}"


async def issue_token_in(db: AsyncSession, user_id: int, purpose: str) -> str:
    """Make the link token inside a transaction the caller already holds."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await db.execute(delete(OneTimeToken).where(OneTimeToken.expires_at <= now))
    await db.execute(
        delete(OneTimeToken).where(OneTimeToken.user_id == user_id, OneTimeToken.purpose == purpose)
    )
    db.add(
        OneTimeToken(
            user_id=user_id,
            purpose=purpose,
            token_hash=hash_token(token),
            created_at=now,
            expires_at=now + LINK_LIFETIMES[purpose],
        )
    )
    return token


async def issue_token(user_id: int, purpose: str) -> str:
    """A new link token for the person. An earlier one for the same purpose stops working.

    A `setup` or `reset` link lasts 24 hours, an `invite` link 72 hours.
    """
    async with write_transaction() as db:
        return await issue_token_in(db, user_id, purpose)


async def find_token(token: str | None, purpose: str) -> OneTimeToken | None:
    """The live link for this token, or None. Does not use it up."""
    if token is None or not _VALUE_SHAPE.fullmatch(token):
        return None
    async with session() as db:
        row = (
            await db.execute(
                select(OneTimeToken).where(
                    OneTimeToken.token_hash == hash_token(token),
                    OneTimeToken.purpose == purpose,
                    OneTimeToken.expires_at > datetime.now(UTC),
                )
            )
        ).scalar_one_or_none()
    return row


async def start_flow(
    request: Request,
    user_id: int,
    kind: str,
    *,
    token_id: int | None = None,
    csrf_secret: str | None = None,
    next_path: str = "",
) -> str:
    """Create a flow (replacing the one the browser already has) and return its cookie value."""
    value = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    async with write_transaction() as db:
        await db.execute(delete(AuthFlow).where(AuthFlow.expires_at <= now))
        if old := request.cookies.get(FLOW_COOKIE):
            await db.execute(delete(AuthFlow).where(AuthFlow.id == hash_token(old)))
        db.add(
            AuthFlow(
                id=hash_token(value),
                user_id=user_id,
                kind=kind,
                token_id=token_id,
                csrf_secret=csrf_secret or csrf.new_secret(),
                next_path=next_path,
                created_at=now,
                expires_at=now + FLOW_LIFETIMES[kind],
            )
        )
    return value


def flow_redirect(location: str, cookie_value: str, kind: str) -> RedirectResponse:
    response = RedirectResponse(location, 303)
    max_age = int(FLOW_LIFETIMES[kind].total_seconds())
    # Setup and reset flows start from a link opened in a mail program, a cross-site navigation, and
    # browsers then withhold `strict` cookies on the redirect that follows: those two use `lax`.
    # Sign-in and enrollment flows start on our own pages and use `strict`.
    samesite: Literal["lax", "strict"] = "lax" if kind in LINK_KINDS else "strict"
    set_cookie(response, FLOW_COOKIE, cookie_value, samesite=samesite, max_age=max_age)
    return response


async def load_flow(request: Request, kinds: tuple[str, ...]) -> Flow:
    """The flow this browser is in, with its person. Raises a page error if there is none."""
    value = request.cookies.get(FLOW_COOKIE, "")
    if not _VALUE_SHAPE.fullmatch(value):
        raise PageError(403, EXPIRED)
    async with session() as db:
        row = (
            await db.execute(
                select(AuthFlow, User)
                .join(User, User.id == AuthFlow.user_id)
                .where(AuthFlow.id == hash_token(value), AuthFlow.expires_at > datetime.now(UTC))
            )
        ).first()
    if row is None or row[0].kind not in kinds:
        raise PageError(403, EXPIRED)
    # A setup or invite flow is for an account that is not active yet; every other flow needs an
    # active one.
    user = row[1]
    usable = user.setup_pending if row[0].kind in SETUP_KINDS else user.is_active
    if not usable:
        raise PageError(403, EXPIRED)
    request.state.csrf_secret = row[0].csrf_secret
    return Flow(row[0], row[1])


async def delete_flow(flow_id: str) -> bool:
    """Remove the flow. False if it was already gone (another request used it)."""
    async with write_transaction() as db:
        result = await db.execute(delete(AuthFlow).where(AuthFlow.id == flow_id))
    return rows_changed(result) == 1


async def attach_session[R: Response](request: Request, user_id: int, response: R) -> R:
    """Finish a sign-in on `response`: a brand-new session id, the old session and flow ended."""
    if old_token := request.cookies.get(SESSION_COOKIE):
        await end_session(old_token)
        forget_confirmation(request, old_token)
    async with write_transaction() as db:
        user = await db.get(User, user_id)
        if user is not None:
            user.last_login_at = datetime.now(UTC)
    clear_cookie(response, LOGIN_COOKIE, samesite="strict")
    clear_cookie(response, FLOW_COOKIE, samesite="lax")
    set_cookie(
        response,
        SESSION_COOKIE,
        await create_session(user_id, request.headers.get("user-agent")),
        samesite="lax",
    )
    return response
