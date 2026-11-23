"""The guest tools through the MCP endpoint: with a fake provider and with the Guesty adapter."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_guest_helpers import (
    GUEST_EMAIL,
    GUEST_ID,
    GUEST_NAME,
    GUESTS_URL,
    NEVER_REQUESTED,
    NEVER_SHOWN,
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
from pmsbridge.pms.guesty import guests
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import Guest, GuestQuery, Page
from pmsbridge.pms.testing import FakeProvider

TOOLS = ("list_guests", "get_guest")


class FakeGuests:
    def __init__(self, items: list[Guest]) -> None:
        self.items = items
        self.queries: list[GuestQuery] = []

    async def list_guests(self, query: GuestQuery) -> Page[Guest]:
        self.queries.append(query)
        return Page[Guest](items=self.items, next_cursor=None)

    async def get_guest(self, guest_id: str) -> Guest:
        for item in self.items:
            if item.id == guest_id:
                return item
        raise PMSNotFoundError("Guest not found.")


class GuestsProvider(FakeProvider):
    def __init__(self, api: FakeGuests) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.GUESTS_READ})

    @property
    def guests(self):  # type: ignore[no-untyped-def]
        return self._api


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""
        self.connection_id = ""

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> AsyncIterator[World]:
    built = World(client)
    user = await seed_user()
    built.connection_id = await seed_connection("Beach houses")
    built.token, _ = await mint_token(user, connections=(built.connection_id,))
    yield built


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, api: FakeGuests) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return GuestsProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(guests.CAPABILITIES),
            {"guests": guests.build(make_client())},
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
    assert set(by_name["list_guests"]["inputSchema"]["properties"]) == {
        "search",
        "limit",
        "cursor",
        "connection",
    }
    assert by_name["get_guest"]["inputSchema"]["required"] == ["guest_id"]
    assert "get_reservation" in by_name["list_guests"]["description"]
    assert "depends on the system" in by_name["list_guests"]["description"]
    for tool in by_name.values():
        if tool["name"] in TOOLS:
            assert "guesty" not in str(tool).lower() and "hostaway" not in str(tool).lower()


async def test_tools_with_a_fake_provider(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeGuests([Guest(id="g1", full_name="Pat Invented", email="pat@example.test")])
    use_fake_provider(monkeypatch, api)
    listed = await world.call("list_guests", search=" pat ", limit=2)
    assert listed["isError"] is False
    assert [item["id"] for item in listed["structuredContent"]["items"]] == ["g1"]
    assert api.queries[-1].search == "pat" and api.queries[-1].limit == 2
    one = await world.call("get_guest", guest_id="g1")
    assert one["structuredContent"]["full_name"] == "Pat Invented"
    blank = await world.call("list_guests", search="  ")
    assert blank["isError"] is False
    assert api.queries[-1].search is None and api.queries[-1].limit == 25


async def test_errors_are_plain_messages(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_provider(monkeypatch, FakeGuests([]))
    missing = await world.call("get_guest", guest_id="nope")
    assert missing["isError"] is True and "not found" in result_text(missing).lower()
    too_long = await world.call("list_guests", cursor="9" * 1001)
    assert too_long["isError"] is True and "cursor" in result_text(too_long)
    long_search = await world.call("list_guests", search="x" * 201)
    assert long_search["isError"] is True and "200" in result_text(long_search)


async def test_a_provider_without_guests_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    for name, arguments in (("list_guests", {}), ("get_guest", {"guest_id": "x"})):
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True and "does not support" in result_text(refused)


async def test_guesty_list_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(GUESTS_URL).mock(return_value=json_response(fixture("list_page.json")))
    result = await world.call("list_guests", limit=3)
    assert result["isError"] is False
    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == "3" and params["skip"] == "0"
    for name in NEVER_REQUESTED:
        assert name not in params["columns"].split(" ")
    page = result["structuredContent"]
    assert len(page["items"]) == 3 and page["total"] == 7 and page["next_cursor"]
    assert page["items"][0]["email"] == GUEST_EMAIL
    shown = str(result)
    for leaked in NEVER_SHOWN:
        assert leaked not in shown
    nxt = await world.call("list_guests", limit=3, cursor=page["next_cursor"])
    assert nxt["isError"] is False
    assert dict(route.calls.last.request.url.params)["skip"] == "3"


async def test_the_page_size_is_kept_within_bounds(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(GUESTS_URL).mock(return_value=json_response({"results": []}))
    await world.call("list_guests", limit=100000)
    assert dict(route.calls.last.request.url.params)["limit"] == "100"
    await world.call("list_guests", limit=0)
    assert dict(route.calls.last.request.url.params)["limit"] == "1"
    await world.call("list_guests")
    assert dict(route.calls.last.request.url.params)["limit"] == "25"


async def test_guesty_search_is_refused_with_no_request(
    world: World, guesty: respx.MockRouter
) -> None:
    route = guesty.route().mock(return_value=json_response({"results": []}))
    result = await world.call("list_guests", search="alex")
    assert result["isError"] is True
    assert "cannot search guests" in result_text(result)
    assert "get_reservation" in result_text(result)
    assert route.call_count == 0


async def test_guesty_get_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(f"{GUESTS_URL}/{GUEST_ID}").mock(
        return_value=json_response(fixture("guest.json"))
    )
    result = await world.call("get_guest", guest_id=GUEST_ID)
    assert result["isError"] is False
    found = result["structuredContent"]
    assert found["full_name"] == GUEST_NAME and found["language"] == "fr"
    assert found["extras"] == {"returning_guest": True}
    assert dict(route.calls.last.request.url.params)["fields"] == guests.GUEST_FIELDS
    shown = str(result)
    for leaked in NEVER_SHOWN:
        assert leaked not in shown


async def test_a_guesty_404_is_reported_plainly(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(f"{GUESTS_URL}/{GUEST_ID}").mock(
        return_value=json_response({"message": "private upstream text"}, 404)
    )
    result = await world.call("get_guest", guest_id=GUEST_ID)
    assert result["isError"] is True
    assert "private upstream text" not in result_text(result)
    assert "not found" in result_text(result).lower()
