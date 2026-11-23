"""Fixtures for the PMS core tests: a migrated database, a fake Guesty and clean shared state."""

from collections.abc import AsyncIterator, Iterator

import pytest
import respx
from argon2 import PasswordHasher
from conn_web import BASE_URL, Browser, Site
from fastapi import FastAPI
from guesty_fakes import FakeGuestyAuth
from pms_helpers import ACCOUNT_ID, CLIENT_ID, CLIENT_SECRET

from pmsbridge.accounts import passwords
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache, get_settings
from pmsbridge.core.storage import migrate, reset_database
from pmsbridge.pms import connections as pms_connections
from pmsbridge.pms import http as pms_http
from pmsbridge.pms.guesty import auth as guesty_auth
from pmsbridge.pms.guesty import client as guesty_client
from pmsbridge.pms.guesty import provider as guesty_provider


@pytest.fixture(autouse=True)
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("test"))
    clear_settings_cache()
    migrate(get_settings())
    yield
    await pms_http.close_http_clients()
    await reset_database()


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    guesty_auth.reset_token_state()
    guesty_client.reset_throttles()
    guesty_provider.reset_discovery()
    pms_connections.reset_limits()
    yield
    guesty_auth.reset_token_state()
    guesty_client.reset_throttles()
    guesty_provider.reset_discovery()
    pms_connections.reset_limits()


@pytest.fixture
def mock_guesty() -> Iterator[respx.MockRouter]:
    """Every outgoing request is intercepted. An unmocked address fails the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def fake_auth(mock_guesty: respx.MockRouter) -> FakeGuestyAuth:
    fake = FakeGuestyAuth(mock_guesty)
    fake.add_account(CLIENT_ID, CLIENT_SECRET, ACCOUNT_ID)
    return fake


@pytest.fixture
def app(database: None, monkeypatch: pytest.MonkeyPatch, mock_guesty: respx.MockRouter) -> FastAPI:
    """The application with accounts, OAuth and connections (a fake Guesty is the only network)."""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    clear_settings_cache()
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(passwords, "_dummy_hash", None)
    return create_app(
        (
            "pmsbridge.core.web",
            "pmsbridge.accounts.web",
            "pmsbridge.oauth.web",
            "pmsbridge.pms.connections",
        )
    )


@pytest.fixture
async def browsers(app: FastAPI) -> AsyncIterator[Browser]:
    opened: list[Site] = []

    def build(address: str = "203.0.113.5") -> Site:
        site = Site(app, address)
        opened.append(site)
        return site

    yield build
    for site in opened:
        await site.client.aclose()
