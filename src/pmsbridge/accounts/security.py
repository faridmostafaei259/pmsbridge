"""Two-factor pages: the second step of sign-in, enrollment, and the account security page.

Sign-in is two steps. After a correct password the visitor is put in a five-minute server-side
flow (see `flows.py`) and asks for a code; only a correct code issues a session, and its id is new.
An administrator without two-factor never gets a session: the flow sends them to enrollment.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pmsbridge.accounts import passwords, totp, twofactor
from pmsbridge.accounts.auth import (
    CurrentUser,
    PageError,
    clear_cookie,
    current_user_or_none,
    read_form,
    require_user,
    safe_next,
    verify_csrf,
)
from pmsbridge.accounts.db import AuthFlow
from pmsbridge.accounts.flows import (
    EXPIRED,
    FLOW_COOKIE,
    SETUP_KINDS,
    Flow,
    attach_session,
    delete_flow,
    flow_redirect,
    load_flow,
    rows_changed,
    start_flow,
)
from pmsbridge.accounts.pages import render
from pmsbridge.accounts.service import find_user_by_email
from pmsbridge.accounts.setup import finalize_link
from pmsbridge.core.crypto import VaultError
from pmsbridge.core.storage import write_transaction

SignedIn = Annotated[CurrentUser, Depends(require_user)]

logger = logging.getLogger("pmsbridge.accounts")

SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
BAD_CODE = "That code is not correct. Try again."
BAD_REAUTH = "The password or the code is not correct."
NO_VAULT = "Two-factor sign-in cannot be set up: the server has no usable encryption key."
ADMIN_MUST_KEEP = "Administrators must keep two-factor sign-in on."


def locked_message(wait: int) -> str:
    """The lockout message, with the real wait (15 minutes, or up to a day after many failures)."""
    minutes = max(1, -(-wait // SECONDS_PER_MINUTE))
    if minutes < MINUTES_PER_HOUR:
        span = f"{minutes} minute{'s' if minutes != 1 else ''}"
    else:
        hours = -(-minutes // MINUTES_PER_HOUR)
        span = f"{hours} hour{'s' if hours != 1 else ''}"
    return f"Too many wrong codes. Try again in about {span}."


class CodeForm(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, extra="ignore")

    code: str = Field(default="", max_length=64)


class ReauthForm(CodeForm):
    password: str = Field(default="", max_length=passwords.MAX_PASSWORD_LENGTH)


def _code_form[M: CodeForm](model: type[M], raw: dict[str, str]) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError:
        return model()  # never log the error: it describes the submitted fields


async def attempt(
    request: Request, user_id: int, check: Callable[[], Awaitable[bool]]
) -> tuple[bool, int]:
    """Run a code check under the per-person limit. Returns (accepted, seconds to wait)."""
    limiter = cast(twofactor.CodeLimiter, request.app.state.code_limiter)
    key = twofactor.limiter_key(user_id)
    if wait := limiter.reserve(key):
        logger.warning("second-factor check refused: too many failures")
        return False, wait
    try:
        accepted = await check()
    except BaseException:
        limiter.release(key)
        raise
    if accepted:
        limiter.succeed(key)
    else:
        limiter.record_failure(key)
        logger.warning("second-factor check failed", extra={"user_id": user_id})
    return accepted, 0


def limited_page(
    request: Request,
    template: str,
    *,
    error: str | None = None,
    status: int = 200,
    wait: int = 0,
    **context: object,
) -> Response:
    response = render(
        request,
        template,
        status_code=429 if wait else status,
        error=locked_message(wait) if wait else error,
        **context,
    )
    if wait:
        response.headers["Retry-After"] = str(wait)
    return response


def _recovery_page(request: Request, codes: list[str], continue_url: str) -> Response:
    return render(request, "accounts/recovery.html", codes=codes, continue_url=continue_url)


async def _enrollment(request: Request) -> Flow:
    flow = await load_flow(request, (*SETUP_KINDS, "enroll", "enable"))
    kind = flow.flow.kind
    if kind in SETUP_KINDS and flow.flow.password_hash is None:
        raise PageError(400, "Choose a password first.")
    if kind not in SETUP_KINDS and await twofactor.has_totp(flow.user.id):
        raise PageError(400, "Two-factor sign-in is already on for this account.")
    if kind == "enable":
        current = await current_user_or_none(request)
        if current is None or current.user_id != flow.user.id:
            raise PageError(403, EXPIRED)
    return flow


async def _pending_secret(flow: Flow) -> bytes:
    """The secret being enrolled. Made on first use and kept (encrypted) in the flow."""
    try:
        async with write_transaction() as db:
            stored = await db.get(AuthFlow, flow.flow.id)
            if stored is None:
                raise PageError(403, EXPIRED)
            if stored.totp_secret is None:
                stored.totp_secret = twofactor.seal_pending_secret(totp.new_secret(), stored.id)
            return twofactor.open_pending_secret(stored.totp_secret, stored.id)
    except VaultError:
        logger.error("two-factor enrollment failed: the encryption key is not usable")
        raise PageError(503, NO_VAULT) from None


async def _enroll_page(request: Request, flow: Flow, **kwargs: object) -> Response:
    secret = await _pending_secret(flow)
    uri = totp.otpauth_uri(secret, flow.user.email)
    return limited_page(
        request,
        "accounts/enroll.html",
        qr=totp.qr_svg(uri),
        secret=totp.secret_text(secret),
        uri=uri,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


async def _finish_enrollment(request: Request, flow: Flow, secret: bytes, step: int) -> Response:
    kind, user_id = flow.flow.kind, flow.user.id
    if kind in SETUP_KINDS:
        codes = await finalize_link(flow, totp_secret=secret, step=step)
        response = _recovery_page(request, codes, "/login")
    else:
        async with write_transaction() as db:
            gone = await db.execute(delete(AuthFlow).where(AuthFlow.id == flow.flow.id))
            if rows_changed(gone) != 1:
                raise PageError(403, EXPIRED)
            twofactor.add_totp(db, user_id, secret, step)
            codes = await twofactor.replace_recovery_codes(db, user_id)
        if kind == "enroll":
            response = _recovery_page(request, codes, safe_next(flow.flow.next_path))
            await attach_session(request, user_id, response)
        else:
            response = _recovery_page(request, codes, "/account/security")
    clear_cookie(response, FLOW_COOKIE, samesite="lax")
    logger.info("two-factor sign-in enabled", extra={"user_id": user_id})
    return response


async def confirm_identity(
    request: Request, user: CurrentUser, raw: dict[str, str]
) -> tuple[bool, int]:
    """Check the signed-in person's password and a code, under the per-person limit.

    Returns (confirmed, seconds to wait). Mistakes count towards the same limit as the sign-in
    code step.
    """
    form = _code_form(ReauthForm, raw)
    stored = await find_user_by_email(user.email)

    async def check() -> bool:
        return (
            stored is not None
            and await passwords.verify_password(stored.password_hash, form.password)
            and await twofactor.use_code(user.user_id, form.code)
        )

    return await attempt(request, user.user_id, check)


async def _reauthenticate(
    request: Request, user: CurrentUser, raw: dict[str, str]
) -> Response | None:
    """None if the password and a code are right; otherwise the page to send back."""
    accepted, wait = await confirm_identity(request, user, raw)
    if accepted:
        return None
    return await _security_page(request, user, error=BAD_REAUTH, status=400, wait=wait)


async def _security_page(
    request: Request,
    user: CurrentUser,
    *,
    error: str | None = None,
    status: int = 200,
    wait: int = 0,
) -> Response:
    return limited_page(
        request,
        "accounts/security.html",
        error=error,
        status=status,
        wait=wait,
        enabled=await twofactor.has_totp(user.user_id),
        remaining=await twofactor.remaining_recovery_codes(user.user_id),
        is_admin=user.role == "admin",
    )


def register_routes(app: FastAPI) -> None:
    app.state.code_limiter = twofactor.new_code_limiter()

    @app.get("/login/verify", include_in_schema=False)
    async def verify_page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        flow = await load_flow(request, ("second_factor",))
        return limited_page(request, "accounts/verify.html", email=flow.user.email)

    @app.post("/login/verify", include_in_schema=False)
    async def verify_submit(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        raw = await read_form(request)
        flow = await load_flow(request, ("second_factor",))
        await verify_csrf(request, raw, secret=flow.flow.csrf_secret)
        code = _code_form(CodeForm, raw).code
        accepted, wait = await attempt(
            request, flow.user.id, lambda: twofactor.use_code(flow.user.id, code)
        )
        if not accepted:
            return limited_page(
                request,
                "accounts/verify.html",
                email=flow.user.email,
                error=BAD_CODE,
                status=400,
                wait=wait,
            )
        if not await delete_flow(flow.flow.id):  # another request already used this sign-in
            raise PageError(403, EXPIRED)
        response = RedirectResponse(safe_next(flow.flow.next_path), 303)
        logger.info("user signed in", extra={"user_id": flow.user.id})
        return await attach_session(request, flow.user.id, response)

    @app.get("/two-factor/enroll", include_in_schema=False)
    async def enroll_page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _enroll_page(request, await _enrollment(request))

    @app.post("/two-factor/enroll", include_in_schema=False)
    async def enroll_submit(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        raw = await read_form(request)
        flow = await _enrollment(request)
        await verify_csrf(request, raw, secret=flow.flow.csrf_secret)
        secret = await _pending_secret(flow)
        step = totp.matching_step(secret, _code_form(CodeForm, raw).code.replace(" ", ""))

        async def check() -> bool:
            return step is not None

        accepted, wait = await attempt(request, flow.user.id, check)
        if not accepted or step is None:
            return await _enroll_page(request, flow, error=BAD_CODE, status=400, wait=wait)
        return await _finish_enrollment(request, flow, secret, step)

    @app.get("/account/security", include_in_schema=False)
    async def security_page(request: Request, user: SignedIn) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _security_page(request, user)

    @app.post("/account/security/enable", include_in_schema=False)
    async def enable(request: Request, user: SignedIn) -> Response:  # pyright: ignore[reportUnusedFunction]
        await verify_csrf(request, await read_form(request))
        if await twofactor.has_totp(user.user_id):
            return RedirectResponse("/account/security", 303)
        cookie = await start_flow(
            request, user.user_id, "enable", csrf_secret=str(request.state.csrf_secret)
        )
        return flow_redirect("/two-factor/enroll", cookie, "enable")

    @app.post("/account/security/disable", include_in_schema=False)
    async def disable(request: Request, user: SignedIn) -> Response:  # pyright: ignore[reportUnusedFunction]
        raw = await read_form(request)
        await verify_csrf(request, raw)
        if user.role == "admin":
            raise PageError(403, ADMIN_MUST_KEEP)
        if refused := await _reauthenticate(request, user, raw):
            return refused
        async with write_transaction() as db:
            await twofactor.remove_two_factor(db, user.user_id)
        logger.info("two-factor sign-in disabled", extra={"user_id": user.user_id})
        return RedirectResponse("/account/security", 303)

    @app.post("/account/security/recovery-codes", include_in_schema=False)
    async def regenerate(request: Request, user: SignedIn) -> Response:  # pyright: ignore[reportUnusedFunction]
        raw = await read_form(request)
        await verify_csrf(request, raw)
        if not await twofactor.has_totp(user.user_id):
            raise PageError(400, "Turn on two-factor sign-in first.")
        if refused := await _reauthenticate(request, user, raw):
            return refused
        async with write_transaction() as db:
            codes = await twofactor.replace_recovery_codes(db, user.user_id)
        logger.info("recovery codes regenerated", extra={"user_id": user.user_id})
        return _recovery_page(request, codes, "/account/security")
