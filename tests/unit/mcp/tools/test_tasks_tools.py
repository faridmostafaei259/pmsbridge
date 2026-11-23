"""The task tools through the MCP endpoint: with a fake provider and with the Guesty adapter."""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_task_helpers import (
    CLEANING_ID,
    CREATE_URL,
    CREATED_ID,
    DRAFT_TEXT,
    INJECTION_TEXT,
    LISTING_ID,
    NEVER_SHOWN,
    TASKS_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)
from mcp_helpers import call_tool, mint_token, result_text, rpc, seed_connection, seed_user

from pmsbridge.mcp import framework
from pmsbridge.pms import registry
from pmsbridge.pms.base import Capability, ConnectionRef, PMSProvider, Provider
from pmsbridge.pms.guesty import tasks
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import NewTask, Page, Task, TaskQuery, TaskStatus, TaskType
from pmsbridge.pms.testing import FakeProvider

TOOLS = ("list_tasks", "create_task")
CREATE_DESCRIPTION = "Creates a task, for example a cleaning task. Show the person the exact title,"
FUTURE = "2099-12-01T10:00:00Z"


class FakeTasks:
    def __init__(self) -> None:
        self.queries: list[TaskQuery] = []
        self.created: list[NewTask] = []

    async def list_tasks(self, query: TaskQuery) -> Page[Task]:
        self.queries.append(query)
        return Page[Task](items=[Task(id="t1", title="Clean")])

    async def create_task(self, task: NewTask) -> Task:
        self.created.append(task)
        return Task(id="t2", title=task.title, task_type=task.task_type)


class TasksProvider(FakeProvider):
    def __init__(self, api: FakeTasks) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TASKS_READ, Capability.TASKS_CREATE})

    @property
    def tasks(self):  # type: ignore[no-untyped-def]
        return self._api


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""
        self.loads: list[str] = []

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        arguments = {"connection": "Beach houses", **arguments}
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> AsyncIterator[World]:
    built = World(client)
    user = await seed_user()
    connection_id = await seed_connection("Beach houses", writes_enabled=True)
    read_only = await seed_connection("Read only", writes_enabled=False)
    built.token, _ = await mint_token(user, connections=(connection_id, read_only))
    yield built


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, world: World, api: FakeTasks) -> None:
    async def load(connection_id: str) -> PMSProvider:
        world.loads.append(connection_id)
        return TasksProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(tasks.CAPABILITIES),
            {"tasks": tasks.build(make_client())},
        )

    monkeypatch.setattr(registry, "load_provider", load)
    with mock_router() as router:
        yield router


async def listed_tools(world: World) -> dict[str, Any]:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    return {tool["name"]: tool for tool in listed}


# ---- How the tools are published ----


async def test_listing_is_read_only_and_creating_is_a_gated_change(world: World) -> None:
    tools = await listed_tools(world)
    assert tools["list_tasks"]["annotations"]["readOnlyHint"] is True
    assert tools["create_task"]["annotations"]["readOnlyHint"] is False
    assert "changes data" in tools["create_task"]["description"]
    for name in TOOLS:
        assert "connection" in tools[name]["inputSchema"]["properties"]
        text = str(tools[name]).lower()
        assert "guesty" not in text and "hostaway" not in text


async def test_the_descriptions_tell_the_assistant_what_to_do(world: World) -> None:
    tools = await listed_tools(world)

    def words(name: str) -> str:
        return " ".join(tools[name]["description"].split())

    assert words("create_task").startswith(CREATE_DESCRIPTION)
    assert "outcome is unknown" in words("create_task") and "twice" in words("create_task")
    reading = words("list_tasks")
    assert "untrusted" in reading and "never as instructions" in reading
    assert set(tools["list_tasks"]["inputSchema"]["properties"]) == {
        "listing_id",
        "status",
        "task_type",
        "due_from",
        "due_to",
        "limit",
        "cursor",
        "connection",
    }
    assert tools["create_task"]["inputSchema"]["required"] == ["title"]


# ---- With a fake provider ----


async def test_list_passes_the_filters_and_keeps_the_page_size_in_bounds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeTasks()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call(
        "list_tasks",
        listing_id=" L1 ",
        status="open",
        task_type="cleaning",
        due_from="2026-03-01",
        due_to="2026-03-31",
        limit=2,
    )
    assert result["isError"] is False
    assert [t["id"] for t in result["structuredContent"]["items"]] == ["t1"]
    query = api.queries[-1]
    assert query.listing_id == "L1" and query.status is TaskStatus.OPEN
    assert query.task_type is TaskType.CLEANING and query.limit == 2
    assert str(query.due_from) == "2026-03-01" and str(query.due_to) == "2026-03-31"
    await world.call("list_tasks", limit=100000, listing_id="  ")
    assert api.queries[-1].limit == 100 and api.queries[-1].listing_id is None
    await world.call("list_tasks", limit=0)
    assert api.queries[-1].limit == 1
    await world.call("list_tasks")
    assert api.queries[-1].limit == 25 and api.queries[-1].status is None


async def test_a_long_cursor_a_bad_filter_or_a_backwards_range_is_refused_plainly(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeTasks()
    use_fake_provider(monkeypatch, world, api)
    long_cursor = await world.call("list_tasks", cursor="9" * 1001)
    assert long_cursor["isError"] is True and "cursor" in result_text(long_cursor)
    long_filter = await world.call("list_tasks", listing_id="x" * 201)
    assert long_filter["isError"] is True and "200" in result_text(long_filter)
    for arguments in ({"due_from": "yesterday"}, {"status": "nonsense"}, {"task_type": "nonsense"}):
        assert (await world.call("list_tasks", **arguments))["isError"] is True
    backwards = await world.call("list_tasks", due_from="2026-03-31", due_to="2026-03-01")
    assert backwards["isError"] is True and "due_to" in result_text(backwards)
    assert api.queries == []


async def test_create_builds_the_canonical_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeTasks()
    use_fake_provider(monkeypatch, world, api)
    made = await world.call(
        "create_task",
        title="Clean",
        description="Kitchen",
        task_type="cleaning",
        checklist=["Linen", "Soap"],
        listing_id=" L1 ",
        reservation_id="R1",
        due_at=FUTURE,
    )
    assert made["isError"] is False and made["structuredContent"]["id"] == "t2"
    sent = api.created[-1]
    assert sent.title == "Clean" and sent.task_type is TaskType.CLEANING
    assert sent.checklist == ["Linen", "Soap"] and sent.listing_id == "L1"
    assert sent.reservation_id == "R1" and sent.due_at is not None
    assert sent.due_at.isoformat().startswith("2099-12-01T10:00:00")


BAD_INPUTS: list[tuple[dict[str, object], str]] = [
    ({"title": ""}, "title must be between 1 and 200"),
    ({"title": "x" * 201}, "title must be between 1 and 200"),
    ({"title": "T", "description": "d" * 5001}, "description must be between 1 and 5000"),
    ({"title": "T", "checklist": ["ok", ""]}, "checklist can have at most 50"),
    ({"title": "T", "checklist": ["x"] * 51}, "checklist can have at most 50"),
    ({"title": "T", "due_at": "2099-12-01T10:00:00"}, "time zone"),
    ({"title": "T", "listing_id": "x" * 201}, "id is not valid"),
]


@pytest.mark.parametrize(("arguments", "expected"), BAD_INPUTS)
async def test_a_bad_input_is_refused_without_repeating_the_text(
    world: World, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object], expected: str
) -> None:
    api = FakeTasks()
    use_fake_provider(monkeypatch, world, api)
    refused = await world.call("create_task", **arguments)
    assert refused["isError"] is True
    assert expected in result_text(refused)
    assert "xxxxxxxx" not in result_text(refused)
    assert api.created == []


@pytest.mark.parametrize(
    "arguments", [{"title": "T", "due_at": "soon"}, {"title": "T", "task_type": "nonsense"}]
)
async def test_an_unreadable_value_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object]
) -> None:
    api = FakeTasks()
    use_fake_provider(monkeypatch, world, api)
    assert (await world.call("create_task", **arguments))["isError"] is True
    assert api.created == []


# ---- The change switch and the write limit ----


async def test_creating_is_refused_where_changes_are_not_allowed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeTasks()
    use_fake_provider(monkeypatch, world, api)
    refused = await world.call("create_task", connection="Read only", title="Clean")
    assert refused["isError"] is True
    assert "Changes are not allowed on the connection" in result_text(refused)
    assert world.loads == [] and api.created == []
    reading = await world.call("list_tasks", connection="Read only")
    assert reading["isError"] is False


async def test_creating_has_its_own_limit_for_each_approval(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeTasks()
    use_fake_provider(monkeypatch, world, api)
    for _ in range(framework.WRITES_BURST):
        assert (await world.call("create_task", title="Clean"))["isError"] is False
    refused = await world.call("create_task", title="Clean")
    assert refused["isError"] is True and "changes too quickly" in result_text(refused)
    assert len(api.created) == framework.WRITES_BURST
    assert (await world.call("list_tasks"))["isError"] is False


async def test_a_provider_without_tasks_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    for name, arguments in (("list_tasks", {}), ("create_task", {"title": "Clean"})):
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True and "does not support" in result_text(refused)


# ---- With the Guesty adapter ----


async def test_guesty_list_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(TASKS_URL).mock(
        side_effect=[json_response(fixture("list_page.json")), json_response([])]
    )
    result = await world.call("list_tasks", limit=2, status="open", task_type="cleaning")
    assert result["isError"] is False, result_text(result)
    assert "filters" in dict(route.calls.last.request.url.params)
    page = result["structuredContent"]
    assert [t["id"] for t in page["items"]] == [CLEANING_ID]
    first = page["items"][0]
    assert first["task_type"] == "cleaning" and first["status"] == "open"
    assert first["listing_id"] == LISTING_ID and first["assignee_name"] is None
    for leaked in NEVER_SHOWN:
        assert leaked not in str(result)


async def test_guesty_list_passes_text_on_as_data(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(TASKS_URL).mock(
        side_effect=[json_response(fixture("list_page.json")), json_response([])]
    )
    result = await world.call("list_tasks", status="cancelled", limit=1)
    assert result["isError"] is False
    assert result["structuredContent"]["items"][0]["title"] == INJECTION_TEXT


async def test_guesty_create_end_to_end_is_one_post(world: World, guesty: respx.MockRouter) -> None:
    posts = guesty.post(CREATE_URL).mock(
        return_value=json_response(fixture("create_response.json"))
    )
    result = await world.call(
        "create_task",
        title="Deep clean before arrival",
        task_type="cleaning",
        checklist=["Wash the linen", "Restock soap", "Check the oven"],
        listing_id=LISTING_ID,
        due_at=FUTURE,
    )
    assert result["isError"] is False, result_text(result)
    assert posts.call_count == 1
    body = json.loads(posts.calls.last.request.content)
    assert body["type"] == "cleaning" and body["checklist"][0] == "Wash the linen"
    created = result["structuredContent"]
    assert created["id"] == CREATED_ID and created["checklist"][0] == {
        "text": "Wash the linen",
        "done": True,
    }


async def test_guesty_timeout_reports_an_unknown_outcome_and_is_not_resent(
    world: World, guesty: respx.MockRouter
) -> None:
    posts = guesty.post(CREATE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    result = await world.call("create_task", title="Clean")
    assert result["isError"] is True
    assert "outcome is unknown" in result_text(result) and posts.call_count == 1


async def test_guesty_failures_do_not_leak_upstream_text_or_the_draft(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.post(CREATE_URL).mock(return_value=json_response({"message": "private upstream"}, 400))
    result = await world.call("create_task", title=DRAFT_TEXT)
    assert result["isError"] is True
    assert "private upstream" not in result_text(result) and DRAFT_TEXT not in result_text(result)
