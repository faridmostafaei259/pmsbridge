"""PMS connections: the dashboard pages, the actions behind them, and the consent-screen list.

Everyone signed in sees the list of connections (name, system, status, token budget, last
successful call, whether changes are allowed). Administrators add Guesty connections, replace
their credentials, test, turn off and on, remove, and allow or stop changes. The same actions back
the `connections` admin commands (`pmsbridge.admin.connections`).

Rules that protect the company's Guesty token budget (5 new tokens per 24 hours per API key):

- Adding or replacing credentials uses one token. The key is checked against the other
  connections first, so a duplicate never costs a token, and the token Guesty issues is stored as
  the connection's token.
- Replacing credentials goes through `guesty.auth.replace_credentials` (the connection lock; the
  old credentials stay until the new ones are accepted; the connection id stays).
- Testing sends one read-only request with the stored token and never asks for a token.
- Adds and replacements are limited to a few per day (in memory, so per process).

The client secret comes only from a password field (or a hidden prompt). It is never put in a
page, a redirect, a log line or an error message. Adding, replacing, removing and allowing changes
need the password-and-code confirmation used for other sensitive administrator actions.
"""

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, cast

from fastapi import Depends, FastAPI
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pmsbridge.accounts.auth import (
    CurrentUser,
    PageError,
    read_form,
    require_admin,
    require_user,
    verify_csrf,
)
from pmsbridge.accounts.management import ActionRefusedError
from pmsbridge.accounts.pages import render
from pmsbridge.accounts.reauth import ReauthStore
from pmsbridge.accounts.security import BAD_REAUTH, confirm_identity, limited_page
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import consent
from pmsbridge.oauth.db import OAuthGrant
from pmsbridge.pms.base import PMSError, Provider
from pmsbridge.pms.db import (
    LABEL_MAX_LENGTH,
    PMSConnection,
    PMSToken,
    client_id_in_use,
    open_token,
    sealed_credential_columns,
)
from pmsbridge.pms.guesty import auth as guesty_auth
from pmsbridge.pms.guesty import provider as guesty_provider
from pmsbridge.pms.guesty.client import throttle_for
from pmsbridge.pms.http import close_http_clients, get_http_client

logger = logging.getLogger(__name__)

SignedIn = Annotated[CurrentUser, Depends(require_user)]
Admin = Annotated[CurrentUser, Depends(require_admin)]

MAX_CREDENTIAL_LENGTH: Final = 256
MAX_FIELD_LENGTH: Final = 300
MAX_LISTED: Final = 200
ADDS_PER_DAY: Final = 5
REPLACEMENTS_PER_DAY: Final = 3
DAY_SECONDS: Final = 86400
TEST_SECONDS: Final = 25.0
MIN_TEST_TOKEN_LIFE: Final = timedelta(seconds=60)
_ID = re.compile(r"[0-9a-f]{32}")
STATUS_TEXT: Final = {
    "active": "Working",
    "token_budget_low": "Token budget low",
    "needs_attention": "Needs attention",
    "disabled": "Turned off",
}
USABLE: Final = ("active", "token_budget_low")
NOTICES: Final = {
    "added": "The connection was added.",
    "replaced": "The credentials were replaced. The connection keeps its name and its approvals.",
    "removed": "The connection was removed.",
    "disabled": "The connection is turned off.",
    "enabled": "The connection is turned on.",
    "writes-on": "Changes are now allowed on this connection.",
    "writes-off": "Changes are no longer allowed on this connection.",
}
ALLOW_CHANGES_WARNING: Final = (
    "The AI assistant will be able to change data in your Guesty account through this "
    "connection: prices, availability, guest messages and similar. You are responsible for what "
    "it does. Reading stays available either way."
)


@dataclass(frozen=True)
class ConnectionRow:
    id: str
    label: str
    provider: str
    status: str
    hint: str
    writes_enabled: bool
    last_success_at: datetime | None
    budget_remaining: int | None
    budget_reset_at: datetime | None


class AttemptLimit:
    """At most `maximum` attempts per key in any 24 hours, counted in memory."""

    def __init__(self, maximum: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.maximum = maximum
        self._clock = clock
        self._attempts: dict[str, deque[float]] = defaultdict(deque[float])

    def take(self, key: str) -> None:
        now = self._clock()
        recent = self._attempts[key]
        while recent and now - recent[0] >= DAY_SECONDS:
            recent.popleft()
        if len(recent) >= self.maximum:
            hours = max(1, -(-int(recent[0] + DAY_SECONDS - now) // 3600))
            raise ActionRefusedError(
                "Too many attempts today: each one uses one of Guesty's 5 daily tokens. "
                f"Try again in about {hours} hour{'s' if hours != 1 else ''}."
            )
        recent.append(now)


_add_limit = AttemptLimit(ADDS_PER_DAY)
_replace_limit = AttemptLimit(REPLACEMENTS_PER_DAY)
# One add at a time: the checks for a name or key already in use must see the add before it.
_add_lock = asyncio.Lock()
SAVE_FAILED: Final = "Guesty accepted the key but it could not be saved; one daily token was used."


def reset_limits() -> None:
    """Forget the attempt counts and the add lock (tests)."""
    global _add_lock  # noqa: PLW0603
    _add_lock = asyncio.Lock()
    _add_limit._attempts.clear()  # pyright: ignore[reportPrivateUsage]
    _replace_limit._attempts.clear()  # pyright: ignore[reportPrivateUsage]


def _refused(error: PMSError) -> ActionRefusedError:
    return ActionRefusedError(error.message)


def _clean_label(value: str) -> str:
    label = " ".join(value.split())
    if not 1 <= len(label) <= LABEL_MAX_LENGTH or not label.isprintable():
        raise ActionRefusedError(f"Give the connection a name of 1 to {LABEL_MAX_LENGTH} letters.")
    return label


def _clean_credential(value: str, what: str) -> str:
    text = value.strip()
    if not text or len(text) > MAX_CREDENTIAL_LENGTH or not text.isprintable() or " " in text:
        raise ActionRefusedError(f"Enter the {what} exactly as Guesty shows it.")
    return text


async def list_connections() -> list[ConnectionRow]:
    async with session() as db:
        connections = (
            (
                await db.execute(
                    select(PMSConnection).order_by(PMSConnection.label).limit(MAX_LISTED)
                )
            )
            .scalars()
            .all()
        )
        tokens = {
            row.connection_id: row
            for row in (
                await db.execute(
                    select(PMSToken).where(PMSToken.connection_id.in_([c.id for c in connections]))
                )
            ).scalars()
        }
    now = datetime.now(UTC)
    rows: list[ConnectionRow] = []
    for conn in connections:
        token = tokens.get(conn.id)
        known = token is not None and (token.budget_reset_at is None or now < token.budget_reset_at)
        rows.append(
            ConnectionRow(
                conn.id,
                conn.label,
                conn.provider,
                conn.status,
                conn.client_id_hint,
                conn.writes_enabled,
                conn.last_success_at,
                token.budget_remaining if token is not None and known else None,
                token.budget_reset_at if token is not None and known else None,
            )
        )
    return rows


async def active_choices(_user_id: int) -> Sequence[consent.ConnectionChoice]:
    """The connections an assistant may be given, for the consent and connected-apps pages."""
    async with session() as db:
        found = await db.execute(
            select(PMSConnection.id, PMSConnection.label)
            .where(PMSConnection.status.in_(USABLE))
            .order_by(PMSConnection.label)
        )
        return [consent.ConnectionChoice(row.id, row.label) for row in found]


async def find_connection(reference: str) -> PMSConnection:
    """A connection by id, or by name for the admin commands."""
    column = PMSConnection.id if _ID.fullmatch(reference) else PMSConnection.label
    async with session() as db:
        conn = await db.scalar(select(PMSConnection).where(column == reference))
    if conn is None:
        raise ActionRefusedError("There is no connection with this name or id.")
    return conn


def _require_guesty(conn: PMSConnection) -> None:
    if conn.provider != Provider.GUESTY.value:
        raise ActionRefusedError("This action is only available for Guesty connections so far.")


def _audit(action: str, connection_id: str, acting: int | str, **extra: str) -> None:
    logger.info(
        f"connection {action}",
        extra={"acting_user": acting, "connection_id": connection_id, **extra},
    )


async def _store_new(name: str, key: str, secret: str, issued: guesty_auth.IssuedToken) -> str:
    connection_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    try:
        async with write_transaction() as db:
            conn = PMSConnection(
                id=connection_id,
                provider=Provider.GUESTY.value,
                label=name,
                status="active",
                **sealed_credential_columns(
                    connection_id, {"client_id": key, "client_secret": secret}
                ),
                created_at=now,
                updated_at=now,
            )
            db.add(conn)
            await db.flush()
            error = await guesty_auth.store_issued_token(db, conn, issued)
            if error is not None:
                raise _refused(error)  # rolls the new connection back
    except IntegrityError:
        raise ActionRefusedError(
            "A connection with this name, key or Guesty account already exists."
        ) from None
    except SQLAlchemyError as failure:
        logger.error("a new connection could not be saved: %s", type(failure).__name__)
        raise ActionRefusedError(SAVE_FAILED) from None
    return connection_id


async def add_guesty(label: str, client_id: str, client_secret: str, acting: int | str) -> str:
    """Connect a Guesty account; returns the connection id. Uses one of the key's daily tokens."""
    name = _clean_label(label)
    key = _clean_credential(client_id, "client ID")
    secret = _clean_credential(client_secret, "client secret")
    async with _add_lock:
        async with session() as db:
            if await db.scalar(select(PMSConnection.id).where(PMSConnection.label == name)):
                raise ActionRefusedError("A connection with this name already exists.")
            # Before any token is requested: a key that is already connected shares its budget.
            if await client_id_in_use(db, key):
                raise ActionRefusedError("This API key is already connected.")
        _add_limit.take("add")
        try:
            issued = await guesty_auth.validate_credentials(key, secret)
        except PMSError as error:
            raise _refused(error) from None
        connection_id = await _store_new(name, key, secret, issued)
    _audit("added", connection_id, acting)
    return connection_id


async def replace_guesty_credentials(
    reference: str, client_id: str, client_secret: str, acting: int | str
) -> str:
    """Give a connection new credentials, keeping its id. Uses one of the key's daily tokens."""
    conn = await find_connection(reference)
    _require_guesty(conn)
    key = _clean_credential(client_id, "client ID")
    secret = _clean_credential(client_secret, "client secret")
    async with session() as db:
        if await client_id_in_use(db, key, except_connection_id=conn.id):
            raise ActionRefusedError("This API key is already used by another connection.")
    _replace_limit.take(conn.id)
    try:
        await guesty_auth.replace_credentials(conn.id, key, secret)
    except PMSError as error:
        raise _refused(error) from None
    _audit("credentials replaced", conn.id, acting)
    return conn.id


async def test_connection(reference: str, acting: int | str) -> tuple[bool, str]:
    """One read-only request with the stored token. Never asks Guesty for a token."""
    conn = await find_connection(reference)
    ok, message = await _check(conn)
    _audit("tested", conn.id, acting, outcome="ok" if ok else "failed")
    return ok, message


async def _check(conn: PMSConnection) -> tuple[bool, str]:
    _require_guesty(conn)
    async with session() as db:
        row = await db.get(PMSToken, conn.id)
    if row is None or row.expires_at - datetime.now(UTC) <= MIN_TEST_TOKEN_LIFE:
        return True, (
            "No usable access token is stored, so nothing was checked. A new token will be "
            "fetched the next time the connection is used."
        )
    headers = {"Authorization": f"Bearer {open_token(row)}"}
    throttle = throttle_for(conn.guesty_account_id or f"connection:{conn.id}")
    try:
        async with asyncio.timeout(TEST_SECONDS):
            await get_http_client(Provider.GUESTY).request(
                "GET", "/listings", params={"limit": 1, "fields": "_id"}, headers=headers,
                throttle=throttle,
            )  # fmt: skip
    except PMSError as error:
        return (
            False,
            f"Guesty did not accept the check: {error.message} No new token was requested.",
        )
    except TimeoutError:
        return False, "Guesty did not answer in time. No new token was requested."
    async with write_transaction() as db:
        stored = await db.get(PMSConnection, conn.id)
        if stored is not None:
            stored.last_success_at = datetime.now(UTC)
    return True, "Guesty answered. The connection works."


async def _change(
    reference: str, action: str, acting: int | str, change: Callable[[PMSConnection], None]
) -> str:
    found = await find_connection(reference)
    async with write_transaction() as db:
        conn = await db.get(PMSConnection, found.id)
        if conn is None:
            raise ActionRefusedError("This connection no longer exists. Reload the page.")
        change(conn)
        conn.updated_at = datetime.now(UTC)
    _audit(action, found.id, acting)
    return found.id


async def set_enabled(reference: str, enabled: bool, acting: int | str) -> str:
    """Turn a connection off (nothing uses it) or on again."""

    def change(conn: PMSConnection) -> None:
        # Turning on is only for a connection that was turned off. One that needs attention is
        # fixed by replacing its credentials, never by switching it on.
        if conn.status == "needs_attention":
            raise ActionRefusedError(
                "Replace the credentials first: this connection needs attention."
                if enabled
                else "This connection needs attention, so nothing uses it already. "
                "Replace the credentials to bring it back."
            )
        if enabled and conn.status != "disabled":
            raise ActionRefusedError("This connection is already turned on.")
        if not enabled and conn.status == "disabled":
            raise ActionRefusedError("This connection is already turned off.")
        conn.status = "active" if enabled else "disabled"

    return await _change(reference, "enabled" if enabled else "disabled", acting, change)


async def set_writes_enabled(reference: str, allowed: bool, acting: int | str) -> str:
    """Allow or stop changes (write tools) on one connection."""

    def change(conn: PMSConnection) -> None:
        conn.writes_enabled = allowed

    return await _change(
        reference, "changes allowed" if allowed else "changes stopped", acting, change
    )


async def remove_connection(reference: str, acting: int | str) -> str:
    """Delete a connection, its token and credentials, and its id from every assistant approval."""
    found = await find_connection(reference)
    async with write_transaction() as db:
        await db.execute(delete(PMSToken).where(PMSToken.connection_id == found.id))
        await db.execute(delete(PMSConnection).where(PMSConnection.id == found.id))
        live = await db.execute(select(OAuthGrant).where(OAuthGrant.revoked_at.is_(None)))
        for grant in live.scalars():
            if found.id in grant.allowed_connection_ids:
                grant.allowed_connection_ids = [
                    item for item in grant.allowed_connection_ids if item != found.id
                ]
    guesty_auth.forget_connection(found.id)
    _audit("removed", found.id, acting)
    return found.id


# ---- Pages ----


def _field(raw: dict[str, str], name: str) -> str:
    return raw.get(name, "")[:MAX_FIELD_LENGTH]


def _connection_id(raw: dict[str, str]) -> str:
    value = raw.get("connection_id", "")
    if not _ID.fullmatch(value):
        raise PageError(400, "The form could not be read. Reload the page and try again.")
    return value


async def _list_page(
    request: Request,
    user: CurrentUser,
    *,
    notice: str | None = None,
    error: str | None = None,
    status: int = 200,
    result: str | None = None,
) -> Response:
    """`notice` is a key into `NOTICES` (a query value that is not one shows nothing); `result` is
    the text of a Test, made here and never taken from a request."""
    rows = await list_connections()
    return render(
        request,
        "connections/list.html",
        status_code=status,
        rows=rows,
        is_admin=user.role == "admin",
        status_text=STATUS_TEXT,
        notice=result or NOTICES.get(notice or ""),
        error=error,
        truncated=len(rows) >= MAX_LISTED,
        max_listed=MAX_LISTED,
    )


async def _form_page(
    request: Request,
    *,
    replacing: PMSConnection | None = None,
    label: str = "",
    client_id: str = "",
    error: str | None = None,
    notice: str | None = None,
    status: int = 200,
) -> Response:
    return render(
        request,
        "connections/form.html",
        status_code=status,
        replacing=replacing,
        label=label,
        client_id=client_id,
        error=error,
        notice=notice,
    )


async def _guard(
    request: Request,
    admin: CurrentUser,
    raw: dict[str, str],
    *,
    title: str,
    fields: dict[str, str],
    post_path: str,
) -> Response | None:
    """None when the administrator confirmed their password and a code in the last 10 minutes.

    Otherwise the confirmation page to send. Only `fields` are carried through it: a client secret
    never is, so the form asks for it again afterwards.
    """
    store = cast(ReauthStore, request.app.state.reauth)
    session_id = str(request.state.session_info.session_id)
    if store.is_recent(session_id):
        return None

    def confirm_page(*, error: str | None, status: int, wait: int = 0) -> Response:
        return limited_page(
            request,
            "accounts/users_confirm.html",
            error=error,
            status=status,
            wait=wait,
            label=title,
            action=post_path,
            target_email="",
            new_role="",
            fields=list(fields.items()),
            post_path=post_path,
            cancel_path="/connections",
        )

    if raw.get("reauth") != "1":
        return confirm_page(error=None, status=200)
    accepted, wait = await confirm_identity(request, admin, raw)
    if not accepted:
        return confirm_page(error=BAD_REAUTH, status=400, wait=wait)
    store.mark(session_id)
    return None


async def _credentials_post(request: Request, admin: CurrentUser, *, replacing: bool) -> Response:
    raw = await read_form(request)
    await verify_csrf(request, raw)
    label, client_id = _field(raw, "label"), _field(raw, "client_id")
    target: PMSConnection | None = None
    fields = {"label": label, "client_id": client_id}
    path = "/connections/add"
    title = "Add a Guesty connection"
    if replacing:
        try:
            target = await find_connection(_connection_id(raw))
        except ActionRefusedError:
            raise PageError(404, "There is no such connection.") from None
        fields = {"connection_id": target.id, "client_id": client_id}
        path, title = "/connections/rotate", f"Replace the credentials of {target.label}"
    stop = await _guard(request, admin, raw, title=title, fields=fields, post_path=path)
    if stop is not None:
        return stop
    client_secret = raw.get("client_secret", "")[: MAX_CREDENTIAL_LENGTH + 1]
    just_confirmed = raw.get("reauth") == "1"
    del raw
    if not client_secret:
        return await _form_page(
            request,
            replacing=target,
            label=label,
            client_id=client_id,
            notice="Confirmed. Enter the client secret to finish." if just_confirmed else None,
            error=None if just_confirmed else "Enter the client secret.",
        )
    try:
        if target is None:
            await add_guesty(label, client_id, client_secret, admin.user_id)
            return RedirectResponse("/connections?notice=added", 303)
        await replace_guesty_credentials(target.id, client_id, client_secret, admin.user_id)
        return RedirectResponse("/connections?notice=replaced", 303)
    except ActionRefusedError as problem:
        return await _form_page(
            request,
            replacing=target,
            label=label,
            client_id=client_id,
            error=str(problem),
            status=400,
        )


Work = Callable[[str, int | str], Coroutine[object, object, str]]


async def _turn_off(reference: str, acting: int | str) -> str:
    return await set_enabled(reference, False, acting)


async def _turn_on(reference: str, acting: int | str) -> str:
    return await set_enabled(reference, True, acting)


async def _stop_changes(reference: str, acting: int | str) -> str:
    return await set_writes_enabled(reference, False, acting)


async def _allow_changes(reference: str, acting: int | str) -> str:
    return await set_writes_enabled(reference, True, acting)


PLAIN_ACTIONS: Final[tuple[tuple[str, Work, str], ...]] = (
    ("disable", _turn_off, "disabled"),
    ("enable", _turn_on, "enabled"),
    ("stop-changes", _stop_changes, "writes-off"),
)
CONFIRMED_ACTIONS: Final[tuple[tuple[str, Work, str, str], ...]] = (
    ("allow-changes", _allow_changes, "writes-on", ALLOW_CHANGES_WARNING),
    (
        "remove",
        remove_connection,
        "removed",
        "This deletes the connection with its stored credentials and token, and takes it off "
        "every assistant approval. The Guesty API key itself is not touched.",
    ),
)


def register_routes(app: FastAPI) -> None:
    @app.get("/connections", include_in_schema=False)
    async def connection_list(request: Request, user: SignedIn, notice: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _list_page(request, user, notice=notice)

    @app.get("/connections/add", include_in_schema=False)
    async def add_form(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        del admin
        return await _form_page(request)

    @app.post("/connections/add", include_in_schema=False)
    async def add_submit(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _credentials_post(request, admin, replacing=False)

    @app.get("/connections/rotate", include_in_schema=False)
    async def rotate_form(request: Request, admin: Admin, connection: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        del admin
        if not _ID.fullmatch(connection):
            raise PageError(404, "There is no such connection.")
        try:
            target = await find_connection(connection)
        except ActionRefusedError:
            raise PageError(404, "There is no such connection.") from None
        return await _form_page(request, replacing=target)

    @app.post("/connections/rotate", include_in_schema=False)
    async def rotate_submit(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _credentials_post(request, admin, replacing=True)

    @app.post("/connections/test", include_in_schema=False)
    async def test(request: Request, admin: Admin) -> Response:  # pyright: ignore[reportUnusedFunction]
        raw = await read_form(request)
        await verify_csrf(request, raw)
        try:
            ok, message = await test_connection(_connection_id(raw), admin.user_id)
        except ActionRefusedError as problem:
            return await _list_page(request, admin, error=str(problem), status=400)
        if ok:
            return await _list_page(request, admin, result=message)
        return await _list_page(request, admin, error=message)

    for name, plain, plain_notice in PLAIN_ACTIONS:
        _add_plain_action(app, name, plain, plain_notice)
    for name, confirmed, confirmed_notice, warning in CONFIRMED_ACTIONS:
        _add_confirmed_action(app, name, confirmed, confirmed_notice, warning)


def _add_plain_action(app: FastAPI, name: str, work: Work, notice: str) -> None:
    @app.post(f"/connections/{name}", name=f"connection_{name}", include_in_schema=False)
    async def handler(request: Request, admin: Admin) -> Response:
        raw = await read_form(request)
        await verify_csrf(request, raw)
        try:
            await work(_connection_id(raw), admin.user_id)
        except ActionRefusedError as problem:
            return await _list_page(request, admin, error=str(problem), status=400)
        return RedirectResponse(f"/connections?notice={notice}", 303)


def _add_confirmed_action(app: FastAPI, name: str, work: Work, notice: str, warning: str) -> None:
    """A page that explains the action (GET) and an action that needs the recent confirmation."""

    @app.get(f"/connections/{name}", name=f"connection_{name}_page", include_in_schema=False)
    async def explain(request: Request, admin: Admin, connection: str = "") -> Response:
        del admin
        if not _ID.fullmatch(connection):
            raise PageError(404, "There is no such connection.")
        try:
            target = await find_connection(connection)
        except ActionRefusedError:
            raise PageError(404, "There is no such connection.") from None
        return render(
            request,
            "connections/confirm.html",
            action=name,
            target=target,
            warning=warning,
        )

    @app.post(f"/connections/{name}", name=f"connection_{name}", include_in_schema=False)
    async def handler(request: Request, admin: Admin) -> Response:
        raw = await read_form(request)
        await verify_csrf(request, raw)
        reference = _connection_id(raw)
        try:
            target = await find_connection(reference)
        except ActionRefusedError:
            raise PageError(404, "There is no such connection.") from None
        stop = await _guard(
            request,
            admin,
            raw,
            title=f"{name.replace('-', ' ').capitalize()} on {target.label}",
            fields={"connection_id": reference},
            post_path=f"/connections/{name}",
        )
        if stop is not None:
            return stop
        try:
            await work(reference, admin.user_id)
        except ActionRefusedError as problem:
            return await _list_page(request, admin, error=str(problem), status=400)
        return RedirectResponse(f"/connections?notice={notice}", 303)


def _close_clients_on_shutdown(app: FastAPI) -> None:
    """Close the shared PMS HTTP clients when the application stops."""
    wrapped = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
        async with wrapped(application):
            yield
        await close_http_clients()

    app.router.lifespan_context = lifespan


def register(app: FastAPI) -> None:
    """Install the connection pages, the consent-screen list, and domain discovery."""
    consent.set_choice_source(active_choices)
    guesty_provider.discover_domains()
    _close_clients_on_shutdown(app)
    register_routes(app)
