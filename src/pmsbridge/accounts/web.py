"""Dashboard sign-in and sign-out, and the signed-in home page.

`register(app)` mounts the routes, the stylesheet and the error handlers. Other features import
`require_user`, `require_admin`, `get_csrf_token` and friends from `pmsbridge.accounts.auth`.
"""

import logging
from urllib.parse import quote

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from pmsbridge.accounts import csrf, passwords, security, setup, twofactor, users_page
from pmsbridge.accounts.auth import (
    LOGIN_COOKIE,
    LOGIN_COOKIE_SECONDS,
    MAX_NEXT_LENGTH,
    SESSION_COOKIE,
    NotAuthenticated,
    PageError,
    clear_cookie,
    current_user_or_none,
    read_form,
    require_user,
    safe_next,
    set_cookie,
    verify_csrf,
)
from pmsbridge.accounts.db import User
from pmsbridge.accounts.flows import attach_session, flow_redirect, start_flow
from pmsbridge.accounts.pages import STATIC_DIR, render
from pmsbridge.accounts.ratelimit import LoginLimiter, client_address_key
from pmsbridge.accounts.reauth import forget_confirmation
from pmsbridge.accounts.service import InvalidEmailError, find_user_by_email, normalise_email
from pmsbridge.accounts.sessions import end_session
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import write_transaction

logger = logging.getLogger("pmsbridge.accounts")

BAD_CREDENTIALS = "Email or password is incorrect."
LOCKED_OUT = "Too many failed sign-in attempts. Wait a few minutes and try again."


class LoginForm(BaseModel):
    """The sign-in form. Input values are kept out of validation errors: they hold a password."""

    model_config = ConfigDict(hide_input_in_errors=True, extra="ignore")

    email: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=passwords.MAX_PASSWORD_LENGTH)
    next: str = Field(default="", max_length=MAX_NEXT_LENGTH)


def _login_secret(request: Request) -> str | None:
    value = request.cookies.get(LOGIN_COOKIE, "")
    return value if len(value) == 2 * csrf.SECRET_BYTES and _is_hex(value) else None


def _is_hex(value: str) -> bool:
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _login_page(
    request: Request,
    secret: str,
    *,
    next_path: str,
    email: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    request.state.csrf_secret = secret
    response = render(
        request,
        "accounts/login.html",
        status_code=status_code,
        next_path=next_path,
        email=email,
        error=error,
    )
    set_cookie(response, LOGIN_COOKIE, secret, samesite="strict", max_age=LOGIN_COOKIE_SECONDS)
    return response


async def _check_credentials(form: LoginForm) -> User | None:
    """The user if the credentials are right. Every failure path costs one password check."""
    try:
        user = await find_user_by_email(normalise_email(form.email))
    except InvalidEmailError:
        user = None
    if user is None:
        await passwords.verify_unknown_user(form.password)
        return None
    if not await passwords.verify_password(user.password_hash, form.password) or not user.is_active:
        return None
    return user


def _locked_page(request: Request, secret: str, next_path: str, email: str, wait: int) -> Response:
    logger.warning("sign-in refused: too many failures")
    response = _login_page(
        request, secret, next_path=next_path, email=email, error=LOCKED_OUT, status_code=429
    )
    response.headers["Retry-After"] = str(wait)
    return response


async def _upgrade_hash(user: User, password: str) -> None:
    """Upgrade the stored hash if its parameters are out of date."""
    if not passwords.needs_rehash(user.password_hash):
        return
    new_hash = await passwords.rehash_password(password)
    async with write_transaction() as db:
        stored = await db.get(User, user.id)
        if stored is not None:
            stored.password_hash = new_hash


async def _login(request: Request, limiter: LoginLimiter) -> Response:
    raw = await read_form(request)
    secret = _login_secret(request)
    if secret is None:
        raise PageError(403, "The form has expired. Reload the page and try again.")
    await verify_csrf(request, raw, secret=secret)
    try:
        # An oversized return path is dropped (the home page is used) rather than failing sign-in.
        form = LoginForm.model_validate({**raw, "next": safe_next(raw.get("next"))})
    except ValidationError:
        # Never log or show the error: it describes the submitted fields.
        form = LoginForm()
    next_path = safe_next(form.next)
    address = client_address_key(request)
    email_key = form.email.strip().lower()
    # The attempt is counted before the password is checked (see ratelimit.py).
    if wait := limiter.reserve(email_key, address):
        return _locked_page(request, secret, next_path, form.email, wait)
    try:
        user = await _check_credentials(form)
        locked = limiter.locked_for(email_key, address)
    except BaseException:
        limiter.release(email_key, address)
        raise
    if user is None:
        limiter.record_failure(email_key, address)
        logger.warning("sign-in failed")
        return _login_page(
            request,
            secret,
            next_path=next_path,
            email=form.email,
            error=BAD_CREDENTIALS,
            status_code=400,
        )
    if locked:
        # A key was locked while this attempt was being checked: refuse even a right password.
        limiter.release(email_key, address)
        return _locked_page(request, secret, next_path, form.email, locked)
    limiter.record_success(email_key, address)
    await _upgrade_hash(user, form.password)
    # Two-factor users, and administrators who must enroll, get a session only after the next step.
    if await twofactor.has_totp(user.id):
        cookie = await start_flow(request, user.id, "second_factor", next_path=next_path)
        return _after_password(flow_redirect("/login/verify", cookie, "second_factor"))
    if user.role == "admin":
        cookie = await start_flow(request, user.id, "enroll", next_path=next_path)
        return _after_password(flow_redirect("/two-factor/enroll", cookie, "enroll"))
    logger.info("user signed in", extra={"user_id": user.id})
    return await attach_session(request, user.id, RedirectResponse(next_path, 303))


def _after_password(response: Response) -> Response:
    clear_cookie(response, LOGIN_COOKIE, samesite="strict")
    return response


def register(app: FastAPI) -> None:
    passwords.prepare_dummy_hash()
    limiter = LoginLimiter()
    app.state.login_limiter = limiter
    setup.register_routes(app)
    security.register_routes(app)
    users_page.register_routes(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, _exc: NotAuthenticated) -> Response:  # pyright: ignore[reportUnusedFunction]
        if request.method in ("GET", "HEAD"):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(safe_next(target), safe='')}", 303)
        return render(
            request,
            "accounts/error.html",
            status_code=401,
            message="Your session has ended. Sign in again.",
        )

    @app.exception_handler(PageError)
    async def _page_error(request: Request, exc: PageError) -> Response:  # pyright: ignore[reportUnusedFunction]
        return render(
            request, "accounts/error.html", status_code=exc.status_code, message=exc.message
        )

    @app.get("/", include_in_schema=False, dependencies=[Depends(require_user)])
    async def home(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return render(request, "accounts/home.html")

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request, next: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        if await current_user_or_none(request) is not None:
            return RedirectResponse(safe_next(next), 303)
        secret = _login_secret(request) or csrf.new_secret()
        return _login_page(request, secret, next_path=safe_next(next))

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _login(request, limiter)

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        response = RedirectResponse("/login", 303)
        if await current_user_or_none(request) is not None:
            await verify_csrf(request, await read_form(request))
            token = request.cookies.get(SESSION_COOKIE, "")
            await end_session(token)
            forget_confirmation(request, token)
            clear_cookie(response, SESSION_COOKIE, samesite="lax")
        elif csrf.origin_allowed(request, get_settings().public_base_url):
            # No live session: only tidy up a stale cookie, and only for a request from this site.
            clear_cookie(response, SESSION_COOKIE, samesite="lax")
        return response
