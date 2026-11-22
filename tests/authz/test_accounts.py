"""Who may do what: access to protected routes, role checks, and sessions that must not work."""

import re
from collections.abc import AsyncIterator, Callable
from typing import Annotated

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport
from sqlalchemy import delete, select, update
from starlette.responses import PlainTextResponse

from pmsbridge.accounts import passwords, totp, twofactor
from pmsbridge.accounts.auth import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    read_form,
    require_admin,
    require_user,
    verify_csrf,
)
from pmsbridge.accounts.db import Session, User, UserTotp
from pmsbridge.accounts.flows import FLOW_COOKIE
from pmsbridge.accounts.service import create_user
from pmsbridge.accounts.sessions import create_session
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import reset_database, session, write_transaction

BASE_URL = "https://pms.example.test"
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
STAFF = ("staff@example.test", "fake staff passphrase")
ADMIN = ("admin@example.test", "fake admin passphrase")
ADMIN_SECRET = bytes(range(20))  # a made-up authenticator secret for the test administrator


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("test"))
    clear_settings_cache()
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(passwords, "_dummy_hash", None)


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app(("pmsbridge.core.web", "pmsbridge.accounts.web"))

    @application.get("/probe/user")
    async def probe_user(user: Annotated[CurrentUser, Depends(require_user)]) -> PlainTextResponse:
        return PlainTextResponse(f"user {user.user_id}")

    @application.get("/probe/admin")
    async def probe_admin(
        user: Annotated[CurrentUser, Depends(require_admin)],
    ) -> PlainTextResponse:
        return PlainTextResponse(f"admin {user.user_id}")

    @application.post("/probe/change")
    async def probe_change(
        request: Request, user: Annotated[CurrentUser, Depends(require_user)]
    ) -> PlainTextResponse:
        await verify_csrf(request, await read_form(request))
        return PlainTextResponse(f"changed by {user.user_id}")

    yield application
    await reset_database()


Browser = Callable[[], httpx.AsyncClient]


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[Browser]:
    opened: list[httpx.AsyncClient] = []

    def new() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.5", 1)), base_url=BASE_URL
        )
        opened.append(client)
        return client

    yield new
    for client in opened:
        await client.aclose()


async def sign_in(
    client: httpx.AsyncClient, email: str, password: str, secret: bytes | None = None
) -> httpx.Response:
    """Sign in; with `secret`, also complete the second step the way an authenticator would."""
    page = await client.get("/login")
    token = CSRF_FIELD.search(page.text)
    assert token
    response = await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token.group(1)},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 303
    if secret is None:
        return response
    assert response.headers["location"] == "/login/verify"
    token = CSRF_FIELD.search((await client.get("/login/verify")).text)
    assert token
    code = totp.code_for(secret, totp.current_step())
    verified = await client.post(
        "/login/verify",
        data={"code": code, "csrf_token": token.group(1)},
        headers={"Origin": BASE_URL},
    )
    assert verified.status_code == 303
    return verified


async def page_token(client: httpx.AsyncClient) -> str:
    match = CSRF_FIELD.search((await client.get("/")).text)
    assert match
    return match.group(1)


@pytest.fixture
async def accounts(app: FastAPI) -> dict[str, int]:
    admin = await create_user(*ADMIN, role="admin")
    async with write_transaction() as db:
        twofactor.add_totp(db, admin, ADMIN_SECRET, 0)
    return {"staff": await create_user(*STAFF, role="staff"), "admin": admin}


async def test_protected_routes_send_visitors_without_a_session_to_sign_in(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    for path in ("/", "/probe/user", "/probe/admin"):
        response = await client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")


async def test_changes_without_a_session_are_refused_not_redirected(
    browser: Browser, accounts: dict[str, int]
) -> None:
    response = await browser().post("/probe/change", data={"x": "y"}, headers={"Origin": BASE_URL})
    assert response.status_code == 401


async def test_staff_cannot_use_admin_routes_and_admins_can(
    browser: Browser, accounts: dict[str, int]
) -> None:
    staff, admin = browser(), browser()
    await sign_in(staff, *STAFF)
    await sign_in(admin, *ADMIN, secret=ADMIN_SECRET)
    assert (await staff.get("/probe/user")).status_code == 200
    assert (await staff.get("/probe/admin")).status_code == 403
    assert (await admin.get("/probe/admin")).text == f"admin {accounts['admin']}"


async def test_guessed_and_malformed_session_cookies_get_nowhere(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    for value in ("a" * 43, "0" * 64, "", "../../etc/passwd", "a" * 5000):
        client.cookies.set(SESSION_COOKIE, value, domain="pms.example.test")
        assert (await client.get("/probe/user")).status_code == 303


async def test_the_stored_session_id_is_not_a_usable_cookie_value(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, *STAFF)
    async with session() as db:
        stored = (await db.execute(select(Session))).scalar_one()
    thief = browser()
    thief.cookies.set(SESSION_COOKIE, stored.id, domain="pms.example.test")
    assert (await thief.get("/probe/user")).status_code == 303


async def test_a_disabled_user_is_locked_out_of_an_open_session(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, *STAFF)
    assert (await client.get("/probe/user")).status_code == 200
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == accounts["staff"]).values(is_active=False))
    assert (await client.get("/probe/user")).status_code == 303


async def test_a_role_change_applies_to_the_next_request(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, *ADMIN, secret=ADMIN_SECRET)
    assert (await client.get("/probe/admin")).status_code == 200
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == accounts["admin"]).values(role="staff"))
    assert (await client.get("/probe/admin")).status_code == 403


async def test_a_csrf_token_belongs_to_one_session(
    browser: Browser, accounts: dict[str, int]
) -> None:
    alice, bob = browser(), browser()
    await sign_in(alice, *STAFF)
    await sign_in(bob, *ADMIN, secret=ADMIN_SECRET)
    alice_token = await page_token(alice)
    headers = {"Origin": BASE_URL}
    good = await alice.post("/probe/change", data={"csrf_token": alice_token}, headers=headers)
    assert good.status_code == 200
    # Alice's token, sent from Bob's browser, must not work.
    stolen = await bob.post("/probe/change", data={"csrf_token": alice_token}, headers=headers)
    assert stolen.status_code == 403
    wrong_origin = await alice.post(
        "/probe/change",
        data={"csrf_token": alice_token},
        headers={"Origin": "https://evil.example"},
    )
    assert wrong_origin.status_code == 403


async def test_the_csrf_header_is_an_alternative_to_the_form_field(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, *STAFF)
    token = await page_token(client)
    response = await client.post(
        "/probe/change",
        data={"x": "y"},
        headers={"Origin": BASE_URL, "X-CSRF-Token": token},
    )
    assert response.status_code == 200


async def test_credentials_in_the_address_do_not_sign_anyone_in(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    response = await client.get(f"/login?email={STAFF[0]}&password=x")
    assert response.status_code == 200
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert (await client.get("/logout")).status_code == 405


PROTECTED = ("/", "/probe/user", "/probe/admin", "/account/security")


async def test_a_password_alone_is_not_enough_for_a_user_with_two_factor(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, *ADMIN, secret=None)  # password step only
    for path in PROTECTED:
        assert (await client.get(path)).status_code == 303, path
    # The sign-in flow cookie is not a session, whatever it is called.
    for name in (FLOW_COOKIE, LOGIN_COOKIE):
        value = client.cookies.get(name) or "a" * 43
        thief = browser()
        thief.cookies.set(SESSION_COOKIE, value, domain="pms.example.test")
        assert (await thief.get("/probe/user")).status_code == 303


async def test_an_administrator_without_two_factor_never_gets_a_session(
    browser: Browser, accounts: dict[str, int]
) -> None:
    async with write_transaction() as db:
        await db.execute(delete(UserTotp))
    client = browser()
    response = await sign_in(client, *ADMIN, secret=None)
    assert response.headers["location"] == "/two-factor/enroll"
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    for path in PROTECTED:
        assert (await client.get(path)).status_code == 303, path
    async with session() as db:
        assert (await db.execute(select(Session))).scalars().all() == []


async def test_setup_and_reset_pages_give_nothing_without_a_live_link(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    for path in ("/setup", "/reset", "/login/verify", "/two-factor/enroll"):
        assert (await client.get(path)).status_code == 403, path
        post = await client.post(
            path, data={"password": "x" * 20, "confirm": "x" * 20}, headers={"Origin": BASE_URL}
        )
        assert post.status_code == 403, path
    for path in ("/setup", "/reset"):
        assert (await client.get(path, params={"token": "A" * 43})).status_code == 403


async def test_an_administrator_session_without_confirmed_two_factor_is_ended(
    browser: Browser, accounts: dict[str, int]
) -> None:
    """A session made before two-factor existed (or after it was removed) gives no admin access."""
    async with write_transaction() as db:
        await db.execute(delete(UserTotp))
    cookie = await create_session(accounts["admin"])
    client = browser()
    client.cookies.set(SESSION_COOKIE, cookie, domain="pms.example.test")
    for path in PROTECTED:
        assert (await client.get(path)).status_code == 303, path
    async with session() as db:
        assert (await db.execute(select(Session))).scalars().all() == []
    # Staff are not affected: two-factor is optional for them.
    staff = browser()
    staff.cookies.set(
        SESSION_COOKIE, await create_session(accounts["staff"]), domain="pms.example.test"
    )
    assert (await staff.get("/probe/user")).status_code == 200
