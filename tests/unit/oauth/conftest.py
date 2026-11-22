"""Fixtures for the OAuth tests: an app with real migrations and a signed-in person."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport
from oauth_helpers import BASE_URL, EMAIL, PASSWORD, Browser, sign_in

from pmsbridge.accounts import passwords
from pmsbridge.accounts.service import create_user
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import Settings, clear_settings_cache
from pmsbridge.core.storage import reset_database


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
    yield create_app(("pmsbridge.core.web", "pmsbridge.accounts.web", "pmsbridge.oauth.web"))
    await reset_database()


@pytest.fixture
async def make_browser(app: FastAPI) -> AsyncIterator[Browser]:
    opened: list[httpx.AsyncClient] = []

    def build(address: str = "203.0.113.5") -> httpx.AsyncClient:
        transport = ASGITransport(app=app, client=(address, 40000))
        client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        opened.append(client)
        return client

    yield build
    for client in opened:
        await client.aclose()


@pytest.fixture
async def anonymous(make_browser: Browser) -> httpx.AsyncClient:
    return make_browser("203.0.113.5")


@pytest.fixture
async def person(app: FastAPI) -> int:
    return await create_user(EMAIL, PASSWORD)


@pytest.fixture
async def browser(anonymous: httpx.AsyncClient, person: int) -> httpx.AsyncClient:
    """A browser signed in as the (staff) person."""
    response = await sign_in(anonymous)
    assert response.status_code == 303
    return anonymous


@pytest.fixture
def dev_settings(tmp_path: Path) -> Settings:
    return Settings(environment="development", data_dir=tmp_path / "data")
