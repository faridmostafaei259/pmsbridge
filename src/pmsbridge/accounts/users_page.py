"""The users page for administrators: list people, invite, disable, enable, reset, change role.

Every route needs an administrator session. Invite, disable, enable, reset two-factor sign-in and
change role also need the acting administrator to have confirmed their password and a code within
the last 10 minutes (see `reauth.py`): without that, the action's own form comes back as a short
confirmation page that carries the same fields plus a password and a code. Mistakes there count
towards the same limit as the sign-in code step. The rules about who may be changed are in
`management.py`.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Final, cast

from fastapi import Depends, FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pmsbridge.accounts import management
from pmsbridge.accounts.auth import (
    CurrentUser,
    PageError,
    read_form,
    require_admin,
    verify_csrf,
)
from pmsbridge.accounts.flows import link_for
from pmsbridge.accounts.pages import render
from pmsbridge.accounts.reauth import ReauthStore
from pmsbridge.accounts.security import BAD_REAUTH, confirm_identity, limited_page
from pmsbridge.accounts.service import InvalidEmailError
from pmsbridge.core.settings import get_settings

Admin = Annotated[CurrentUser, Depends(require_admin)]

logger = logging.getLogger("pmsbridge.accounts")

MAX_FIELD_LENGTH: Final = 300
_USER_ID = re.compile(r"[1-9][0-9]{0,17}")
NOTICES: Final = {
    "disabled": "The account is disabled and the person is signed out everywhere.",
    "enabled": "The account is active again.",
    "reset": "Two-factor sign-in was reset and the person is signed out everywhere.",
    "reset-admin": (
        "Two-factor sign-in was reset and the person is signed out everywhere. "
        "They must set it up again at their next sign-in."
    ),
    "role": "The role was changed.",
    "promoted": "The role was changed and the person is signed out everywhere.",
    "revoked": "The invitation was cancelled.",
}
ACTIONS: Final = {
    "invite": ("Invite a person", ("email", "role")),
    "disable": ("Disable an account", ("user_id",)),
    "enable": ("Turn an account on again", ("user_id",)),
    "reset-2fa": ("Reset two-factor sign-in", ("user_id",)),
    "role": ("Change a role", ("user_id", "role")),
}


async def _users_page(
    request: Request,
    admin: CurrentUser,
    *,
    error: str | None = None,
    notice: str | None = None,
    status: int = 200,
) -> Response:
    listed = await management.list_users()
    return render(
        request,
        "accounts/users.html",
        status_code=status,
        error=error,
        notice=NOTICES.get(notice or ""),
        rows=listed.rows,
        truncated=listed.truncated,
        max_listed=management.MAX_LISTED,
        admin_id=admin.user_id,
    )


async def _guarded(
    request: Request, admin: CurrentUser, action: str
) -> tuple[dict[str, str], Response | None]:
    """Check CSRF and the recent confirmation. Returns the form values, or the page to send."""
    raw = await read_form(request)
    await verify_csrf(request, raw)
    label, fields = ACTIONS[action]
    values = {name: raw.get(name, "")[:MAX_FIELD_LENGTH] for name in fields}
    store = cast(ReauthStore, request.app.state.reauth)
    session_id = str(request.state.session_info.session_id)
    if store.is_recent(session_id):
        return values, None

    target_email = values.get("email", "").strip()
    if _USER_ID.fullmatch(values.get("user_id", "")):
        target_email = await management.email_of(int(values["user_id"])) or ""

    def confirm_page(*, error: str | None, status: int, wait: int = 0) -> Response:
        return limited_page(
            request,
            "accounts/users_confirm.html",
            error=error,
            status=status,
            wait=wait,
            label=label,
            action=action,
            target_email=target_email,
            new_role=values.get("role", "") if action in ("role", "invite") else "",
            fields=list(values.items()),
        )

    if raw.get("reauth") != "1":
        return values, confirm_page(error=None, status=200)
    accepted, wait = await confirm_identity(request, admin, raw)
    if not accepted:
        return values, confirm_page(error=BAD_REAUTH, status=400, wait=wait)
    store.mark(session_id)
    return values, None


def _user_id(values: dict[str, str]) -> int:
    text = values["user_id"]
    if not _USER_ID.fullmatch(text):
        raise PageError(400, "The form could not be read. Reload the page and try again.")
    return int(text)


async def _perform(
    request: Request, admin: CurrentUser, work: Callable[[], Awaitable[str]]
) -> Response:
    """Run an action; a refusal comes back on the users page, success redirects with a notice."""
    try:
        notice = await work()
    except (management.ActionRefusedError, InvalidEmailError) as problem:
        return await _users_page(request, admin, error=str(problem), status=400)
    return RedirectResponse(f"/admin/users?notice={notice}", 303)


def register_routes(app: FastAPI) -> None:
    app.state.reauth = ReauthStore()

    @app.get("/admin/users", include_in_schema=False)
    async def users(request: Request, admin: Admin, notice: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _users_page(request, admin, notice=notice)

    @app.post("/admin/users/invite", include_in_schema=False)
    async def invite(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        values, stop = await _guarded(request, admin, "invite")
        if stop is not None:
            return stop
        try:
            token = await management.invite_user(values["email"], values["role"], admin.user_id)
        except (management.ActionRefusedError, InvalidEmailError) as problem:
            return await _users_page(request, admin, error=str(problem), status=400)
        link = link_for("/invite", token, get_settings().public_base_url)
        return render(request, "accounts/invite_created.html", link=link, role=values["role"])

    @app.post("/admin/users/disable", include_in_schema=False)
    async def disable(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        values, stop = await _guarded(request, admin, "disable")
        if stop is not None:
            return stop

        async def work() -> str:
            await management.disable_user(_user_id(values), admin.user_id)
            return "disabled"

        return await _perform(request, admin, work)

    @app.post("/admin/users/enable", include_in_schema=False)
    async def enable(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        values, stop = await _guarded(request, admin, "enable")
        if stop is not None:
            return stop

        async def work() -> str:
            await management.enable_user(_user_id(values), admin.user_id)
            return "enabled"

        return await _perform(request, admin, work)

    @app.post("/admin/users/reset-2fa", include_in_schema=False)
    async def reset_two_factor(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        values, stop = await _guarded(request, admin, "reset-2fa")
        if stop is not None:
            return stop

        async def work() -> str:
            was_admin = await management.reset_two_factor(_user_id(values), admin.user_id)
            return "reset-admin" if was_admin else "reset"

        return await _perform(request, admin, work)

    @app.post("/admin/users/role", include_in_schema=False)
    async def role(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        values, stop = await _guarded(request, admin, "role")
        if stop is not None:
            return stop

        async def work() -> str:
            await management.change_role(_user_id(values), values["role"], admin.user_id)
            return "promoted" if values["role"] == "admin" else "role"

        return await _perform(request, admin, work)

    @app.post("/admin/users/revoke", include_in_schema=False)
    async def revoke(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        raw = await read_form(request)
        await verify_csrf(request, raw)

        async def work() -> str:
            await management.revoke_invite(
                _user_id({"user_id": raw.get("user_id", "")}), admin.user_id
            )
            return "revoked"

        return await _perform(request, admin, work)
