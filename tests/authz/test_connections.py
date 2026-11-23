"""Who may see and change PMS connections, and what a signed-in person never learns."""

import re
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import pytest
import respx
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from pmsbridge.accounts import management, passwords, totp, twofactor
from pmsbridge.accounts.service import create_user
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import reset_database, session, write_transaction
from pmsbridge.pms.db import PMSConnection, sealed_credential_columns

BASE_URL = "https://pms.example.test"
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
STAFF = ("staff@example.test", "fake staff passphrase")
ADMIN = ("admin@example.test", "fake admin passphrase")
ADMIN_SECRET = bytes(range(20))
SECRET_VALUE = "authz-secret-value-not-real"  # noqa: S105
CLIENT_ID_VALUE = "0authz-client-id-4321"
ADMIN_ACTIONS = (
    "add",
    "rotate",
    "test",
    "disable",
    "enable",
    "remove",
    "allow-changes",
    "stop-changes",
)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("test"))
    clear_settings_cache()
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(passwords, "_dummy_hash", None)


@pytest.fixture(autouse=True)
def _no_network() -> Iterator[respx.MockRouter]:  # an outgoing request would fail the test
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    yield create_app(
        (
            "pmsbridge.core.web",
            "pmsbridge.accounts.web",
            "pmsbridge.oauth.web",
            "pmsbridge.pms.connections",
        )
    )
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


def _token(text: str) -> str:
    match = CSRF_FIELD.search(text)
    assert match
    return match.group(1)


async def sign_in(client: httpx.AsyncClient, credentials: tuple[str, str], with_code: bool) -> None:
    page = await client.get("/login")
    await client.post(
        "/login",
        data={"email": credentials[0], "password": credentials[1], "csrf_token": _token(page.text)},
        headers={"Origin": BASE_URL},
    )
    if with_code:
        verify = await client.get("/login/verify")
        await client.post(
            "/login/verify",
            data={
                "code": totp.code_for(ADMIN_SECRET, totp.current_step()),
                "csrf_token": _token(verify.text),
            },
            headers={"Origin": BASE_URL},
        )


async def post(client: httpx.AsyncClient, path: str, data: dict[str, str]) -> httpx.Response:
    fields = {**data, "csrf_token": _token((await client.get("/connections")).text)}
    return await client.post(
        path,
        content=urlencode(fields),
        headers={"Origin": BASE_URL, "Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.fixture
async def connection_id(app: FastAPI) -> str:
    identifier = secrets.token_hex(16)
    now = datetime.now(UTC)
    async with write_transaction() as db:
        db.add(
            PMSConnection(
                id=identifier,
                provider="guesty",
                label="Beach houses",
                guesty_account_id="acct-authz",
                created_at=now,
                updated_at=now,
                **sealed_credential_columns(
                    identifier, {"client_id": CLIENT_ID_VALUE, "client_secret": SECRET_VALUE}
                ),
            )
        )
    return identifier


@pytest.fixture
async def people(app: FastAPI) -> None:
    await create_user(*STAFF, role="staff")
    admin = await create_user(*ADMIN, role="admin")
    async with write_transaction() as db:
        twofactor.add_totp(db, admin, ADMIN_SECRET, 0)


async def state(identifier: str) -> tuple[str, bool, str]:
    async with session() as db:
        conn = (
            await db.execute(select(PMSConnection).where(PMSConnection.id == identifier))
        ).scalar_one_or_none()
    assert conn is not None
    return conn.status, conn.writes_enabled, conn.label


async def test_visitors_without_a_session_cannot_see_or_change_connections(
    browser: Browser, connection_id: str, people: None
) -> None:
    client = browser()
    for path in (
        "/connections",
        "/connections/add",
        f"/connections/rotate?connection={connection_id}",
    ):
        response = await client.get(path)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")
    for action in ADMIN_ACTIONS:
        response = await client.post(
            f"/connections/{action}",
            content=urlencode({"connection_id": connection_id}),
            headers={"Origin": BASE_URL, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 401, action
    assert await state(connection_id) == ("active", False, "Beach houses")


async def test_staff_can_read_the_list_and_nothing_else(
    browser: Browser, connection_id: str, people: None
) -> None:
    client = browser()
    await sign_in(client, STAFF, with_code=False)
    page = await client.get("/connections")
    assert page.status_code == 200 and "Beach houses" in page.text
    assert connection_id not in page.text  # staff are never handed connection ids
    assert CLIENT_ID_VALUE not in page.text and SECRET_VALUE not in page.text
    assert "4321" in page.text  # only the last four characters of the key
    for path in (
        "/connections/add",
        f"/connections/rotate?connection={connection_id}",
        f"/connections/remove?connection={connection_id}",
        f"/connections/allow-changes?connection={connection_id}",
    ):
        assert (await client.get(path)).status_code == 403, path
    for action in ADMIN_ACTIONS:
        assert (
            await post(client, f"/connections/{action}", {"connection_id": connection_id})
        ).status_code == 403
    assert await state(connection_id) == ("active", False, "Beach houses")


async def test_an_administrator_must_confirm_before_sensitive_changes(
    browser: Browser, connection_id: str, people: None
) -> None:
    client = browser()
    await sign_in(client, ADMIN, with_code=True)
    for action in ("remove", "allow-changes", "add", "rotate"):
        reply = await post(
            client,
            f"/connections/{action}",
            {"connection_id": connection_id, "label": "x", "client_id": "y", "client_secret": "z"},
        )
        assert reply.status_code == 200 and "Confirm it is you" in reply.text, action
        assert "client_secret" not in reply.text
    assert await state(connection_id) == ("active", False, "Beach houses")


async def test_a_wrong_password_in_the_confirmation_changes_nothing(
    browser: Browser, connection_id: str, people: None
) -> None:
    client = browser()
    await sign_in(client, ADMIN, with_code=True)
    reply = await post(
        client,
        "/connections/remove",
        {"connection_id": connection_id, "reauth": "1", "password": "wrong", "code": "000000"},
    )
    assert reply.status_code in (400, 429)
    assert await state(connection_id) == ("active", False, "Beach houses")


async def test_no_page_ever_shows_the_stored_credentials(
    browser: Browser, connection_id: str, people: None
) -> None:
    client = browser()
    await sign_in(client, ADMIN, with_code=True)
    for path in (
        "/connections",
        f"/connections/rotate?connection={connection_id}",
        f"/connections/remove?connection={connection_id}",
        f"/connections/allow-changes?connection={connection_id}",
        "/connections/add",
    ):
        page = await client.get(path)
        assert page.status_code == 200, path
        assert SECRET_VALUE not in page.text and CLIENT_ID_VALUE not in page.text, path


async def test_a_disabled_person_loses_access_to_the_pages(
    browser: Browser, connection_id: str, people: None
) -> None:
    client = browser()
    await sign_in(client, STAFF, with_code=False)
    assert (await client.get("/connections")).status_code == 200
    staff = await management.user_by_email(STAFF[0])
    await management.disable_user(staff.id, None)
    assert (await client.get("/connections")).status_code == 303
