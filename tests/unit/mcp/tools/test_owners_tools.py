"""The owner tools through the MCP endpoint: with a fake provider and with the Guesty adapter."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_owner_helpers import (
    OWNER_EMAIL,
    OWNER_ID,
    OWNER_NAME,
    OWNER_NEVER_REQUESTED,
    OWNER_NEVER_SHOWN,
    OWNERS_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)
from mcp_helpers import call_tool, mint_token, result_text, rpc, seed_connection, seed_user

from pmsbridge.pms import registry
from pmsbridge.pms.base import (
    Capability,
    ConnectionRef,
    PMSNotFoundError,
    PMSProvider,
    Provider,
)
from pmsbridge.pms.guesty import owners
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import Owner, Page, PageRequest
from pmsbridge.pms.testing import FakeProvider

TOOLS = ("list_owners", "get_owner")


class FakeOwners:
    def __init__(self, items: list[Owner]) -> None:
        self.items = items
        self.requests: list[PageRequest] = []

    async def list_owners(self, page: PageRequest) -> Page[Owner]:
        self.requests.append(page)
        return Page[Owner](items=self.items, next_cursor=None)

    async def get_owner(self, owner_id: str) -> Owner:
        for item in self.items:
            if item.id == owner_id:
                return item
        raise PMSNotFoundError("Owner not found.")


class OwnersProvider(FakeProvider):
    def __init__(self, api: FakeOwners) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.OWNERS_READ})

    @property
    def owners(self):  # type: ignore[no-untyped-def]
        return self._api


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> AsyncIterator[World]:
    built = World(client)
    user = await seed_user()
    connection_id = await seed_connection("Beach houses")
    built.token, _ = await mint_token(user, connections=(connection_id,))
    yield built


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, api: FakeOwners) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return OwnersProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(owners.CAPABILITIES),
            {"owners": owners.build(make_client())},
        )

    monkeypatch.setattr(registry, "load_provider", load)
    with mock_router() as router:
        yield router


async def test_the_tools_are_listed_as_read_only(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in listed}
    for name in TOOLS:
        tool = by_name[name]
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert "connection" in tool["inputSchema"]["properties"]
        assert tool["description"].strip()
        assert "guesty" not in str(tool).lower() and "hostaway" not in str(tool).lower()
        assert "never shown" in tool["description"]
    assert set(by_name["list_owners"]["inputSchema"]["properties"]) == {
        "limit",
        "cursor",
        "connection",
    }
    assert by_name["get_owner"]["inputSchema"]["required"] == ["owner_id"]


async def test_tools_with_a_fake_provider(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeOwners([Owner(id="o1", full_name="Pat Invented", listing_ids=["l1"])])
    use_fake_provider(monkeypatch, api)
    listed = await world.call("list_owners", limit=2)
    assert listed["isError"] is False
    assert [item["id"] for item in listed["structuredContent"]["items"]] == ["o1"]
    assert api.requests[-1].limit == 2
    one = await world.call("get_owner", owner_id="o1")
    assert one["structuredContent"]["listing_ids"] == ["l1"]
    await world.call("list_owners")
    assert api.requests[-1].limit == 25


async def test_errors_are_plain_messages(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_provider(monkeypatch, FakeOwners([]))
    missing = await world.call("get_owner", owner_id="nope")
    assert missing["isError"] is True and "not found" in result_text(missing).lower()
    too_long = await world.call("list_owners", cursor="9" * 1001)
    assert too_long["isError"] is True and "cursor" in result_text(too_long)


async def test_a_provider_without_owners_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    for name, arguments in (("list_owners", {}), ("get_owner", {"owner_id": "x"})):
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True and "does not support" in result_text(refused)


async def test_guesty_list_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(OWNERS_URL).mock(
        return_value=json_response(fixture("owners", "list_page.json"))
    )
    result = await world.call("list_owners", limit=3)
    assert result["isError"] is False
    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == "3" and params["skip"] == "0"
    for name in OWNER_NEVER_REQUESTED:
        assert name not in params["fields"].split(" ")
    page = result["structuredContent"]
    assert len(page["items"]) == 3 and page["total"] == 7 and page["next_cursor"]
    assert page["items"][0]["email"] == OWNER_EMAIL
    shown = str(result)
    for leaked in OWNER_NEVER_SHOWN:
        assert leaked not in shown
    nxt = await world.call("list_owners", limit=3, cursor=page["next_cursor"])
    assert nxt["isError"] is False
    assert dict(route.calls.last.request.url.params)["skip"] == "3"


async def test_the_page_size_is_kept_within_bounds(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(OWNERS_URL).mock(return_value=json_response({"results": []}))
    await world.call("list_owners", limit=100000)
    assert dict(route.calls.last.request.url.params)["limit"] == "100"
    await world.call("list_owners", limit=0)
    assert dict(route.calls.last.request.url.params)["limit"] == "1"


async def test_guesty_get_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response(fixture("owners", "owner.json"))
    )
    result = await world.call("get_owner", owner_id=OWNER_ID)
    assert result["isError"] is False
    found = result["structuredContent"]
    assert found["full_name"] == OWNER_NAME and len(found["listing_ids"]) == 2
    assert dict(route.calls.last.request.url.params)["fields"] == owners.OWNER_FIELDS
    shown = str(result)
    for leaked in OWNER_NEVER_SHOWN:
        assert leaked not in shown


async def test_a_guesty_404_is_reported_plainly(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(f"{OWNERS_URL}/{OWNER_ID}").mock(
        return_value=json_response({"error": {"message": "private upstream text"}}, 404)
    )
    result = await world.call("get_owner", owner_id=OWNER_ID)
    assert result["isError"] is True
    assert "private upstream text" not in result_text(result)
    assert "not found" in result_text(result).lower()
