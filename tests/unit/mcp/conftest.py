"""Fixtures for the MCP tests: an app with real migrations, the MCP endpoint and a web client."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport
from mcp_helpers import BASE_URL

from pmsbridge.accounts import passwords
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import reset_database
from pmsbridge.mcp.framework import reset_rate_limits
from pmsbridge.pms.guesty import client as guesty_client

FEATURES = ("pmsbridge.core.web", "pmsbridge.oauth.web", "pmsbridge.mcp.server")


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
def _fresh_rate_limits() -> None:
    reset_rate_limits()
    guesty_client.reset_throttles()


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app(FEATURES)
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan() -> None:
        # The transport's task group must be entered and left by the same task.
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    await started.wait()
    yield application
    stop.set()
    await task
    await reset_database()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app, client=("198.51.100.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as opened:
        yield opened
