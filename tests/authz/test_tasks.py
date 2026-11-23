"""Who may use the task tools: only people with a valid token, on connections it allows.

A connection the token does not allow gets exactly the answer an unknown one gets, no provider is
built for it, and nothing about it appears in any answer. Creating a task also needs the
connection's change switch.
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
from pmsbridge.pms.models import NewTask, Page, Task, TaskQuery
from pmsbridge.pms.testing import FakeProvider

SECRET_LABEL = "Confidential owner portfolio"  # noqa: S105
TOOL_ARGUMENTS: dict[str, dict[str, object]] = {
    "list_tasks": {},
    "create_task": {"title": "Clean the flat"},
}
TASK = Task(id="t1", title="Private task words")


class FakeTasks:
    def __init__(self) -> None:
        self.created: list[NewTask] = []

    async def list_tasks(self, query: TaskQuery) -> Page[Task]:
        return Page[Task](items=[TASK])

    async def create_task(self, task: NewTask) -> Task:
        self.created.append(task)
        return TASK


class TasksProvider(FakeProvider):
    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TASKS_READ, Capability.TASKS_CREATE})

    @property
    def tasks(self):  # type: ignore[no-untyped-def]
        return FakeTasks()


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
        return TasksProvider([], "x")

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
    mine = await seed_connection("Beach houses", writes_enabled=True)
    secret = await seed_connection(SECRET_LABEL, writes_enabled=True)
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
async def test_a_staff_member_with_an_approved_connection_can_use_the_tools(
    client: httpx.AsyncClient, loads: list[str], tool: str
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses", writes_enabled=True)
    await seed_connection(SECRET_LABEL, writes_enabled=True)
    token, _ = await mint_token(ada, connections=(mine,))
    result = await call(client, token, tool, **TOOL_ARGUMENTS[tool])
    assert result["isError"] is False, result_text(result)
    assert loads == [mine]
    assert SECRET_LABEL not in str(result)


async def test_an_approved_connection_without_the_change_switch_can_read_but_not_change(
    client: httpx.AsyncClient, loads: list[str]
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses", writes_enabled=False)
    token, _ = await mint_token(ada, connections=(mine,))
    for tool in ("list_tasks",):
        assert (await call(client, token, tool, **TOOL_ARGUMENTS[tool]))["isError"] is False
    loads.clear()
    for tool in ("create_task",):
        refused = await call(client, token, tool, **TOOL_ARGUMENTS[tool])
        assert refused["isError"] is True and "Changes are not allowed" in result_text(refused)
    assert loads == []


async def test_with_two_connections_the_tools_need_the_name_and_only_use_approved_ones(
    client: httpx.AsyncClient, loads: list[str]
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    first = await seed_connection("Beach houses", writes_enabled=True)
    second = await seed_connection("City flats", writes_enabled=True)
    token, _ = await mint_token(ada, connections=(first, second))
    unclear = await call(client, token, "create_task", **TOOL_ARGUMENTS["create_task"])
    assert unclear["isError"] is True and loads == []
    chosen = await call(
        client,
        token,
        "create_task",
        connection="City flats",
        **TOOL_ARGUMENTS["create_task"],
    )
    assert chosen["isError"] is False and loads == [second]


@pytest.mark.parametrize("tool", sorted(TOOL_ARGUMENTS))
async def test_a_revoked_grant_is_refused(
    client: httpx.AsyncClient, loads: list[str], tool: str
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses", writes_enabled=True)
    token, _ = await mint_token(ada, connections=(mine,), revoke_grant=True)
    response = await rpc(
        client, token, "tools/call", {"name": tool, "arguments": TOOL_ARGUMENTS[tool]}
    )
    assert response.status_code == 401 and loads == []
    assert "Private task words" not in response.text


async def test_a_disabled_person_cannot_create_a_task(
    client: httpx.AsyncClient, loads: list[str]
) -> None:
    ada = await seed_user("ada@example.test", role="staff")
    mine = await seed_connection("Beach houses", writes_enabled=True)
    token, _ = await mint_token(ada, connections=(mine,))
    await disable_user(ada)
    response = await rpc(
        client,
        token,
        "tools/call",
        {"name": "create_task", "arguments": TOOL_ARGUMENTS["create_task"]},
    )
    assert response.status_code == 401 and loads == []


async def test_no_token_means_no_tasks(client: httpx.AsyncClient, loads: list[str]) -> None:
    for tool, arguments in TOOL_ARGUMENTS.items():
        response = await rpc(client, None, "tools/call", {"name": tool, "arguments": arguments})
        assert response.status_code == 401
        assert "Private task words" not in response.text
    assert loads == []
