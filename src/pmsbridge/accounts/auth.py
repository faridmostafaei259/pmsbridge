"""What other features use to know who is signed in: `require_user`, `require_admin`, CSRF checks.

Typical use in a dashboard route:

    @app.post("/things")
    async def create_thing(request: Request, user: Annotated[CurrentUser, Depends(require_user)]):
        form = await read_form(request)
        await verify_csrf(request, form)
        ...

Pages put `get_csrf_token(request)` in a hidden field named `csrf_token`. Routes that are called by
programs rather than by a browser form (the OAuth token endpoint, the MCP endpoint) do not use the
session cookie and must not use these checks.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final, Literal, cast
from urllib.parse import parse_qsl

from fastapi import Depends
from starlette.requests import Request
from starlette.responses import Response

from pmsbridge.accounts import csrf
from pmsbridge.accounts.db import UserTotp
from pmsbridge.accounts.sessions import SessionInfo, end_session, load_session
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import session

SESSION_COOKIE: Final = "__Host-pmsbridge_session"
LOGIN_COOKIE: Final = "__Host-pmsbridge_login"
LOGIN_COOKIE_SECONDS: Final = 3600
MAX_FORM_BYTES: Final = 16 * 1024
MAX_FORM_FIELDS: Final = 50
MAX_NEXT_LENGTH: Final = 2048
_FORBIDDEN_IN_PATH: Final = "\\\x7f"
_FORM_TYPE: Final = "application/x-www-form-urlencoded"
_SameSite = Literal["lax", "strict"]


@dataclass(frozen=True)
class CurrentUser:
    user_id: int
    email: str
    role: str


class NotAuthenticated(Exception):  # noqa: N818  (reads better at the call site)
    """No valid session. The web layer turns it into a redirect to the sign-in page."""


class PageError(Exception):
    """An error to show to the person as a plain page. `message` is safe to display."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def set_cookie(
    response: Response, name: str, value: str, *, samesite: _SameSite, max_age: int | None = None
) -> None:
    """Always Secure and HttpOnly, with Path=/ and no Domain, as the `__Host-` prefix requires."""
    response.set_cookie(
        name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite=samesite
    )


def clear_cookie(response: Response, name: str, *, samesite: _SameSite) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite=samesite)


async def _has_totp(user_id: int) -> bool:
    async with session() as db:
        return await db.get(UserTotp, user_id) is not None


async def _session_info(request: Request) -> SessionInfo | None:
    """The session for this request, looked up once per request."""
    state = request.state
    if not hasattr(state, "session_info"):
        token = request.cookies.get(SESSION_COOKIE)
        info = await load_session(token)
        if info is not None and info.role == "admin" and not await _has_totp(info.user_id):
            # An administrator session needs confirmed two-factor. One made before that existed
            # (or after it was removed) ends here; signing in again leads to enrollment.
            await end_session(token or "")
            info = None
        state.session_info = info
        if info is not None:
            state.csrf_secret = info.csrf_secret
    return cast("SessionInfo | None", state.session_info)


async def current_user_or_none(request: Request) -> CurrentUser | None:
    info = await _session_info(request)
    return None if info is None else CurrentUser(info.user_id, info.email, info.role)


async def require_user(request: Request) -> CurrentUser:
    user = await current_user_or_none(request)
    if user is None:
        raise NotAuthenticated
    return user


async def require_admin(user: Annotated[CurrentUser, Depends(require_user)]) -> CurrentUser:
    if user.role != "admin":
        raise PageError(403, "Only an administrator can do this.")
    return user


def get_csrf_token(request: Request) -> str:
    """A token for a hidden form field. Call after `require_user` has resolved the session."""
    secret = getattr(request.state, "csrf_secret", None)
    if not isinstance(secret, str):
        raise RuntimeError("no CSRF secret for this request; resolve the session first")
    return csrf.mask_token(secret)


async def read_form(request: Request) -> dict[str, str]:
    """Read a small URL-encoded form body. The first value of each field wins."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != _FORM_TYPE:
        raise PageError(400, "The form could not be read. Reload the page and try again.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            raise PageError(413, "The form is too large.")
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS)
    except ValueError:
        raise PageError(400, "The form could not be read. Reload the page and try again.") from None
    form: dict[str, str] = {}
    for name, value in pairs:
        form.setdefault(name, value)
    return form


async def verify_csrf(
    request: Request, form: Mapping[str, str] | None = None, *, secret: str | None = None
) -> None:
    """Refuse the request unless Origin and the CSRF token are right. Use on every state change.

    `secret` is for pages shown before sign-in; otherwise the session's own secret is used.
    """
    if not csrf.origin_allowed(request, get_settings().public_base_url):
        raise PageError(403, "The request came from an unexpected address. Reload and try again.")
    if secret is None:
        info = await _session_info(request)
        secret = None if info is None else info.csrf_secret
    token = (form or {}).get(csrf.CSRF_FIELD) or request.headers.get(csrf.CSRF_HEADER)
    if secret is None or not csrf.token_matches(secret, token):
        raise PageError(403, "The form has expired. Reload the page and try again.")


def safe_next(value: str | None) -> str:
    """A local path to return to after sign-in; anything else becomes `/`. No open redirects."""
    if (
        not value
        or len(value) > MAX_NEXT_LENGTH
        or not value.startswith("/")
        or value.startswith("//")
        or any(char in _FORBIDDEN_IN_PATH or char <= " " for char in value)
    ):
        return "/"
    return value
