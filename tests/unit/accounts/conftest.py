"""Fixtures for the account tests: an app with real migrations in a temporary directory."""

import base64
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport

from pmsbridge.accounts import passwords, totp, twofactor
from pmsbridge.accounts.service import create_user
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import Settings, clear_settings_cache
from pmsbridge.core.storage import reset_database, write_transaction

# The hasher the application really uses, captured before any test swaps in a cheaper one.
REAL_HASHER = passwords._hasher  # pyright: ignore[reportPrivateUsage]
BASE_URL = "https://pms.example.test"
EMAIL = "ada@example.test"
PASSWORD = "fake-passphrase-for-tests"  # noqa: S105
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
SECRET_TEXT = re.compile(r'<code class="secret">([A-Z2-7]+)</code>')
RECOVERY_CODE = re.compile(r"<code>([A-Z2-7]{5}(?:-[A-Z2-7]{5}){3})</code>")


@pytest.fixture(autouse=True)
def fast_hasher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cheap argon2 parameters so that the tests are quick; the hashing tests pick their own."""
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(passwords, "_dummy_hash", None)


@pytest.fixture(autouse=True)
def public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("test"))
    clear_settings_cache()


@pytest.fixture
def dev_settings(tmp_path: Path) -> Settings:
    return Settings(environment="development", data_dir=tmp_path / "data")


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    yield create_app(("pmsbridge.core.web", "pmsbridge.accounts.web"))
    await reset_database()


MakeClient = Callable[[str], httpx.AsyncClient]


@pytest.fixture
async def make_client(app: FastAPI) -> AsyncIterator[MakeClient]:
    """Build browsers that connect from a chosen network address."""
    clients: list[httpx.AsyncClient] = []

    def build(address: str = "203.0.113.5") -> httpx.AsyncClient:
        transport = ASGITransport(app=app, client=(address, 40000))
        client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        clients.append(client)
        return client

    yield build
    for client in clients:
        await client.aclose()


@pytest.fixture
async def client(make_client: MakeClient) -> httpx.AsyncClient:
    return make_client("203.0.113.5")


@pytest.fixture
async def user_id(app: FastAPI) -> int:
    return await create_user(EMAIL, PASSWORD)


def page_token(html: str) -> str:
    match = CSRF_FIELD.search(html)
    assert match, "the page has no CSRF field"
    return match.group(1)


async def sign_in(
    client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD, next_path: str = ""
) -> httpx.Response:
    """Load the sign-in page and submit it the way a browser would (starting signed out)."""
    client.cookies.clear()
    page = await client.get("/login")
    return await client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": page_token(page.text),
            "next": next_path,
        },
        headers={"Origin": BASE_URL},
    )


def current_code(secret: bytes, offset: int = 0) -> str:
    return totp.code_for(secret, totp.current_step() + offset)


def secret_from_page(html: str) -> bytes:
    match = SECRET_TEXT.search(html)
    assert match, "the page shows no authenticator key"
    text = match.group(1)
    return base64.b32decode(text + "=" * (-len(text) % 8))


async def add_authenticator(user: int, secret: bytes = bytes(range(1, 21))) -> bytes:
    """Give a user a confirmed authenticator directly (no pages), starting with no accepted step."""
    async with write_transaction() as db:
        twofactor.add_totp(db, user, secret, 0)
    return secret


async def post_form(
    client: httpx.AsyncClient, path: str, data: dict[str, str], page: str | None = None
) -> httpx.Response:
    """Load `page` (default: the same path), then post the form with its CSRF token and Origin."""
    html = (await client.get(page or path)).text
    return await client.post(
        path, data={**data, "csrf_token": page_token(html)}, headers={"Origin": BASE_URL}
    )
