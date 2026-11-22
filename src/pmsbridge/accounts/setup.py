"""Setup, invite and password-reset pages, reached from a one-time link.

`/setup?token=...` (new administrator), `/invite?token=...` (invited person) and
`/reset?token=...` (password reset) check the link and
move the browser into a server-side flow, then redirect to the same page without the token. The
token is used up only by the last form of the flow (see `flows.py`). An unknown, used or expired
link all give the same message.
"""

import logging
from datetime import UTC, datetime

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pmsbridge.accounts import passwords
from pmsbridge.accounts.auth import PageError, clear_cookie, read_form, verify_csrf
from pmsbridge.accounts.db import AuthFlow, OneTimeToken, Session, User
from pmsbridge.accounts.flows import (
    FLOW_COOKIE,
    SETUP_KINDS,
    Flow,
    find_token,
    flow_redirect,
    load_flow,
    rows_changed,
    start_flow,
)
from pmsbridge.accounts.pages import render
from pmsbridge.accounts.twofactor import add_totp, replace_recovery_codes
from pmsbridge.core.storage import write_transaction

logger = logging.getLogger("pmsbridge.accounts")

LINK_INVALID = "This link is not valid any more. Ask an administrator for a new one."
PURPOSES = {"/setup": "setup", "/invite": "invite", "/reset": "reset"}


class PasswordForm(BaseModel):
    """The new-password form. Input values stay out of validation errors: they hold a password."""

    model_config = ConfigDict(hide_input_in_errors=True, extra="ignore")

    password: str = Field(default="", max_length=passwords.MAX_PASSWORD_LENGTH + 1)
    confirm: str = Field(default="", max_length=passwords.MAX_PASSWORD_LENGTH + 1)


async def finalize_link(
    flow: Flow, *, totp_secret: bytes | None = None, step: int = 0
) -> list[str]:
    """Use the link up and apply the result. Returns recovery codes (empty when none were made).

    For `setup` and `invite` the account becomes active with the password chosen in the flow (and,
    for an administrator, the confirmed authenticator). For `reset` only the password changes,
    and every session of the person ends. A link that was already used raises a page error.
    """
    async with write_transaction() as db:
        # The link may run out while the flow is open: it must still be live when it is used up.
        used = await db.execute(
            delete(OneTimeToken).where(
                OneTimeToken.id == flow.flow.token_id,
                OneTimeToken.expires_at > datetime.now(UTC),
            )
        )
        user = await db.get(User, flow.user.id)
        password_hash = flow.flow.password_hash
        if rows_changed(used) != 1 or user is None or password_hash is None:
            raise PageError(403, LINK_INVALID)
        user.password_hash = password_hash
        if flow.flow.kind == "reset":
            await db.execute(delete(Session).where(Session.user_id == user.id))
            # Also half-finished sign-ins that began with the old password.
            await db.execute(delete(AuthFlow).where(AuthFlow.user_id == user.id))
            codes: list[str] = []
        else:
            user.is_active = True
            user.setup_pending = False
            codes = []
            if totp_secret is not None:
                add_totp(db, user.id, totp_secret, step)
                codes = await replace_recovery_codes(db, user.id)
    message = "password reset completed" if flow.flow.kind == "reset" else "account set up"
    logger.info(message, extra={"user_id": flow.user.id})
    return codes


def register_routes(app: FastAPI) -> None:
    for path, kind in PURPOSES.items():
        _register(app, path, kind)


def _register(app: FastAPI, path: str, kind: str) -> None:
    title = "Choose a new password" if kind == "reset" else "Choose your password"

    def form_page(
        request: Request, flow: Flow, error: str | None = None, status: int = 200
    ) -> Response:
        return render(
            request,
            "accounts/setup.html",
            status_code=status,
            title=title,
            path=path,
            error=error,
            email=flow.user.email,
        )

    @app.get(path, include_in_schema=False)
    async def show(request: Request, token: str | None = None) -> Response:
        if token is not None:
            link = await find_token(token, kind)
            if link is None:
                raise PageError(403, LINK_INVALID)
            cookie = await start_flow(request, link.user_id, kind, token_id=link.id)
            return flow_redirect(path, cookie, kind)
        return form_page(request, await load_flow(request, (kind,)))

    @app.post(path, include_in_schema=False)
    async def submit(request: Request) -> Response:
        raw = await read_form(request)
        flow = await load_flow(request, (kind,))
        await verify_csrf(request, raw, secret=flow.flow.csrf_secret)
        try:
            form = PasswordForm.model_validate(raw)
        except ValidationError:
            return form_page(request, flow, "The password is too long.", 400)  # never log the error
        if form.password != form.confirm:
            return form_page(request, flow, "The two passwords are not the same.", 400)
        try:
            password_hash = await passwords.hash_password(form.password)
        except passwords.PasswordPolicyError as problem:
            return form_page(request, flow, str(problem), 400)
        async with write_transaction() as db:
            stored = await db.get(type(flow.flow), flow.flow.id)
            if stored is None:
                raise PageError(403, LINK_INVALID)
            stored.password_hash = password_hash
        if kind in SETUP_KINDS and flow.user.role == "admin":
            return RedirectResponse("/two-factor/enroll", 303)  # the account stays inactive
        flow.flow.password_hash = password_hash
        await finalize_link(flow)
        done = render(
            request, "accounts/done.html", message="Your password is saved. You can sign in now."
        )
        clear_cookie(done, FLOW_COOKIE, samesite="lax")
        return done
