"""Who may use the owner tools: only people with a valid token, on connections it allows.

A connection the token does not allow gets exactly the answer an unknown one gets, no provider is
built for it, and nothing about it appears in any answer.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport
from mcp_helpers import (
    BASE_URL,
    call_tool,
    disable_user,
    mint_token,
    result_text,
    rpc,
    seed_connection,
    seed_user,
)

from pmsbridge.accounts import passwords
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import reset_database
from pmsbridge.pms import registry
from pmsbridge.pms.base import Capability, PMSProvider
from pmsbridge.pms.models import Owner, Page, PageRequest
from pmsbridge.pms.testing import FakeProvider

SECRET_LABEL = "Confidential owner portfolio"  # noqa: S105
TOOL_ARGUMENTS: dict[str, dict[str, object]] = {
    "list_owners": {},
    "get_owner": {"owner_id": "o1"},
}
OWNER = Owner(id="o1", full_name="Pat Invented", email="pat@example.test")


class FakeOwners:
    async def list_owners(self, page: PageRequest) -> Page[Owner]:
        return Page[Owner](items=[OWNER])

    async def get_owner(self, owner_id: str) -> Owner:
        return OWNER


class OwnersProvider(FakeProvider):
    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.OWNERS_READ})

    @property
    def owners(self):  # type: ignore[no-untyped-def]
        return FakeOwners()


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
async def loads(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[str]]:
    seen: list[str] = []

    async def load(connection_id: str) -> PMSProvider:
        seen.append(connection_id)
        return OwnersProvider([], "x")

    monkeypatch.setattr(registry, "load_provider", load)
    yield seen


@pytest.fixture
async def app(loads: list[str]) -> AsyncIterator[FastAPI]:
    application = create_app(("pmsbridge.core.web", "pmsbridge.oauth.web", "pmsbridge.mcp.server"))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan() -> None:
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
    transport = ASGITransport(app=app, client=("198.51.100.7", 1))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as opened:
        yield opened


async def call(client: httpx.AsyncClient, token: str, tool: str, **arguments: object) -> Any:
    return await call_tool(client, token, tool, arguments)


@pytest.mark.parametrize("tool", sorted(TOOL_ARGUMENTS))
async def test_a_connection_that_is_not_approved_looks_like_an_unknown_one(
    client: httpx.AsyncClient, loads: list[str], tool: str
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses")
    secret = await seed_connection(SECRET_LABEL)
    token, _ = await mint_token(ada, connections=(mine,))
    answers: list[str] = []
    for asked in (secret, SECRET_LABEL, SECRET_LABEL.upper(), "0" * 32, "no such label"):
        result = await call(client, token, tool, connection=asked, **TOOL_ARGUMENTS[tool])
        assert result["isError"] is True
        answers.append(result_text(result).replace(asked, "<asked>"))
    assert len(set(answers)) == 1, answers
    assert SECRET_LABEL not in answers[0] and secret not in answers[0]
    assert loads == []  # no provider was built for a connection the token does not allow


@pytest.mark.parametrize("tool", sorted(TOOL_ARGUMENTS))
async def test_a_staff_member_with_an_approved_connection_can_read(
    client: httpx.AsyncClient, loads: list[str], tool: str
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses")
    await seed_connection(SECRET_LABEL)
    token, _ = await mint_token(ada, connections=(mine,))
    result = await call(client, token, tool, **TOOL_ARGUMENTS[tool])
    assert result["isError"] is False, result_text(result)
    assert loads == [mine]
    assert SECRET_LABEL not in str(result)


async def test_with_two_connections_the_tools_need_the_name_and_only_use_approved_ones(
    client: httpx.AsyncClient, loads: list[str]
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    first = await seed_connection("Beach houses")
    second = await seed_connection("City flats")
    token, _ = await mint_token(ada, connections=(first, second))
    unclear = await call(client, token, "list_owners")
    assert unclear["isError"] is True and loads == []
    chosen = await call(client, token, "list_owners", connection="City flats")
    assert chosen["isError"] is False and loads == [second]


@pytest.mark.parametrize("tool", sorted(TOOL_ARGUMENTS))
async def test_a_revoked_grant_is_refused(
    client: httpx.AsyncClient, loads: list[str], tool: str
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses")
    token, _ = await mint_token(ada, connections=(mine,), revoke_grant=True)
    response = await rpc(
        client, token, "tools/call", {"name": tool, "arguments": TOOL_ARGUMENTS[tool]}
    )
    assert response.status_code == 401
    assert "Pat Invented" not in response.text
    assert loads == []


async def test_a_disabled_person_is_refused(client: httpx.AsyncClient, loads: list[str]) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses")
    token, _ = await mint_token(ada, connections=(mine,))
    await disable_user(ada)
    response = await rpc(client, token, "tools/call", {"name": "list_owners", "arguments": {}})
    assert response.status_code == 401 and loads == []


async def test_no_token_means_no_owners(client: httpx.AsyncClient, loads: list[str]) -> None:
    for tool in TOOL_ARGUMENTS:
        response = await rpc(client, None, "tools/call", {"name": tool, "arguments": {}})
        assert response.status_code == 401
        assert "Pat Invented" not in response.text
    assert loads == []
