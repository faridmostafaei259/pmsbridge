"""Connected apps: the assistants a person has approved, and what they may use.

`/account/apps` is for any signed-in person: it lists their own approvals grouped by assistant
(name, return host, when approved, last used, connections covered), lets them cancel an assistant's
access and change which PMS connections it covers (the same choices as the consent page).
`/admin/assistants` shows every person's approvals to administrators, with each assistant's client
id (the value `oauth revoke-ai-access --client` takes), and lets them cancel approvals after the
recent password-and-code confirmation used for other sensitive administrator actions.

Cancelling revokes the grants, which ends their access and refresh tokens at once. Every change
needs the CSRF token and an `Origin` from this site. A person can only touch their own approvals.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final

from fastapi import Depends, FastAPI
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pmsbridge.accounts.auth import (
    CurrentUser,
    PageError,
    first_values,
    read_form,
    read_form_pairs,
    require_admin,
    require_user,
    verify_csrf,
)
from pmsbridge.accounts.db import User
from pmsbridge.accounts.pages import render
from pmsbridge.accounts.users_page import guarded
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import consent, grants
from pmsbridge.oauth.db import OAuthClient, OAuthGrant
from pmsbridge.oauth.redirects import redirect_host

logger = logging.getLogger("pmsbridge.oauth")

SignedIn = Annotated[CurrentUser, Depends(require_user)]
Admin = Annotated[CurrentUser, Depends(require_admin)]

MAX_LISTED: Final = 500
_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
_USER_ID = re.compile(r"[1-9][0-9]{0,17}")
NOTICES: Final = {
    "revoked": "Access was cancelled. The assistant has to ask for approval again to reconnect.",
    "saved": "The connections were saved.",
}
_STALE = "That assistant is no longer approved. Reload the page."


@dataclass(frozen=True)
class Approval:
    """One person's live approvals for one assistant, merged (approving again adds a grant)."""

    user_id: int
    email: str
    client_id: str
    client_name: str
    hosts: tuple[str, ...]
    approved_at: datetime
    last_used_at: datetime | None
    all_connections: bool
    connection_ids: tuple[str, ...]


async def list_approvals(user_id: int | None = None) -> tuple[list[Approval], bool]:
    """Live approvals, newest first, with True if more exist than the first 500 grants shown."""
    async with session() as db:
        query = (
            select(OAuthGrant, OAuthClient, User.email)
            .join(OAuthClient, OAuthClient.client_id == OAuthGrant.client_id)
            .join(User, User.id == OAuthGrant.user_id)
            .where(OAuthGrant.revoked_at.is_(None))
            .order_by(OAuthGrant.created_at.desc(), OAuthGrant.id.desc())
            .limit(MAX_LISTED + 1)
        )
        if user_id is not None:
            query = query.where(OAuthGrant.user_id == user_id)
        rows = list((await db.execute(query)).all())
        truncated = len(rows) > MAX_LISTED
        rows = rows[:MAX_LISTED]
    merged: dict[tuple[int, str], Approval] = {}
    for grant, client, email in rows:
        key = (grant.user_id, grant.client_id)
        last = grant.last_used_at
        ids = tuple(str(item) for item in grant.allowed_connection_ids)
        before = merged.get(key)
        if before is None:
            merged[key] = Approval(
                grant.user_id,
                email,
                client.client_id,
                client.client_name,
                tuple(dict.fromkeys(redirect_host(uri) for uri in client.redirect_uris)),
                grant.created_at,
                last,
                grant.all_connections,
                ids,
            )
            continue
        newest_use = max((t for t in (before.last_used_at, last) if t is not None), default=None)
        merged[key] = Approval(
            before.user_id,
            before.email,
            before.client_id,
            before.client_name,
            before.hosts,
            before.approved_at,  # rows are newest first
            newest_use,
            before.all_connections or grant.all_connections,
            tuple(dict.fromkeys((*before.connection_ids, *ids))),
        )
    return list(merged.values()), truncated


def coverage(approval: Approval, labels: Mapping[str, str] | None) -> str:
    """What the approval covers, in words. `labels` names connections; None counts them."""
    if approval.all_connections:
        return "All connections, including ones added later"
    if not approval.connection_ids:
        return "No connections"
    if labels is None:
        count = len(approval.connection_ids)
        return f"{count} connection{'s' if count != 1 else ''}"
    names = [labels[item] for item in approval.connection_ids if item in labels]
    gone = len(approval.connection_ids) - len(names)
    if gone:
        names.append(f"{gone} no longer available")
    return ", ".join(names) or "No connections"


async def set_connections(user_id: int, client_id: str, selection: grants.Selection) -> int:
    """Change the connections of a person's live approvals for one assistant; returns how many."""
    async with write_transaction() as db:
        found = (
            await db.execute(
                select(OAuthGrant).where(
                    OAuthGrant.user_id == user_id,
                    OAuthGrant.client_id == client_id,
                    OAuthGrant.revoked_at.is_(None),
                )
            )
        ).scalars()
        changed = list(found)
        for grant in changed:
            grant.all_connections = selection.all_connections
            grant.allowed_connection_ids = list(selection.connection_ids)
        return len(changed)


async def _my_page(
    request: Request,
    user: CurrentUser,
    *,
    notice: str = "",
    error: str | None = None,
    status: int = 200,
) -> Response:
    approvals, truncated = await list_approvals(user.user_id)
    choices = await consent.choices_for(user.user_id)
    labels = {choice.id: choice.label for choice in choices}
    return render(
        request,
        "oauth/apps.html",
        status_code=status,
        rows=[(item, coverage(item, labels), set(item.connection_ids)) for item in approvals],
        truncated=truncated,
        max_listed=MAX_LISTED,
        connections=choices,
        notice=NOTICES.get(notice),
        error=error,
    )


async def _admin_page(request: Request, *, notice: str = "") -> Response:
    approvals, truncated = await list_approvals()
    return render(
        request,
        "oauth/admin_apps.html",
        rows=[(item, coverage(item, None)) for item in approvals],
        truncated=truncated,
        max_listed=MAX_LISTED,
        notice=NOTICES.get(notice),
    )


def _client_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise PageError(400, "The form could not be read. Reload the page and try again.")
    return value


def register_routes(app: FastAPI) -> None:
    @app.get("/account/apps", include_in_schema=False)
    async def my_apps(request: Request, user: SignedIn, notice: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _my_page(request, user, notice=notice)

    @app.post("/account/apps/revoke", include_in_schema=False)
    async def revoke_mine(request: Request, user: SignedIn) -> Response:  # pyright: ignore[reportUnusedFunction]
        form = await read_form(request)
        await verify_csrf(request, form)
        client_id = _client_id(form.get("client_id", ""))
        done = await grants.revoke_grants(user_id=user.user_id, client_id=client_id)
        logger.info(
            "assistant access cancelled",
            extra={"user_id": user.user_id, "client_id": client_id, "grants": done.grants},
        )
        return RedirectResponse("/account/apps?notice=revoked", 303)

    @app.post("/account/apps/connections", include_in_schema=False)
    async def choose_connections(request: Request, user: SignedIn) -> Response:  # pyright: ignore[reportUnusedFunction]
        pairs = await read_form_pairs(request)
        form = first_values(pairs)
        await verify_csrf(request, form)
        client_id = _client_id(form.get("client_id", ""))
        choices = await consent.choices_for(user.user_id)
        selection = consent.read_selection(choices, pairs) if choices else None
        if selection is None:
            return await _my_page(
                request,
                user,
                error="Choose which connections the assistant may use.",
                status=400,
            )
        if await set_connections(user.user_id, client_id, selection) == 0:
            return await _my_page(request, user, error=_STALE, status=400)
        logger.info(
            "assistant connections changed",
            extra={"user_id": user.user_id, "client_id": client_id},
        )
        return RedirectResponse("/account/apps?notice=saved", 303)

    @app.get("/admin/assistants", include_in_schema=False)
    async def all_apps(request: Request, admin: Admin, notice: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        del admin
        return await _admin_page(request, notice=notice)

    @app.post("/admin/assistants/revoke", include_in_schema=False)
    async def revoke_any(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        values, stop = await guarded(
            request,
            admin,
            "revoke-assistant",
            post_path="/admin/assistants/revoke",
            cancel_path="/admin/assistants",
        )
        if stop is not None:
            return stop
        client_id = _client_id(values["client_id"])
        if not _USER_ID.fullmatch(values["user_id"]):
            raise PageError(400, "The form could not be read. Reload the page and try again.")
        target = int(values["user_id"])
        done = await grants.revoke_grants(user_id=target, client_id=client_id)
        logger.info(
            "assistant access cancelled",
            extra={
                "acting_user": admin.user_id,
                "target_user_id": target,
                "client_id": client_id,
                "grants": done.grants,
            },
        )
        return RedirectResponse("/admin/assistants?notice=revoked", 303)
