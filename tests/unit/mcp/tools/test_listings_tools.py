"""The listing tools through the MCP endpoint: with a fake provider and with the Guesty adapters."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_listing_helpers import (
    LISTINGS_URL,
    PHOTOS_URL,
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
from pmsbridge.pms.guesty import content, listings
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import (
    Listing,
    ListingDetails,
    ListingPhoto,
)
from pmsbridge.pms.testing import FakeProvider

ID = "6a1f00000000000000000001"
TOOLS = ("list_listings", "get_listing", "get_listing_details", "list_listing_photos")


class FakeContent:
    async def get_listing_details(self, listing_id: str) -> ListingDetails:
        if listing_id != "known":
            raise PMSNotFoundError("Listing not found.")
        return ListingDetails(listing_id="known", amenities=["Wifi"])

    async def list_listing_photos(self, listing_id: str) -> list[ListingPhoto]:
        return [ListingPhoto(id="p1", url="https://images.example.test/1.jpg", caption="Hall")]


class FakeContentProvider(FakeProvider):
    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {Capability.LISTINGS_READ, Capability.LISTING_DETAILS_READ, Capability.PHOTOS_READ}
        )

    @property
    def content(self):  # type: ignore[no-untyped-def]
        return FakeContent()


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


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, listings_data: list[Listing]) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeContentProvider(listings_data, "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)


def use_guesty_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load(connection_id: str) -> PMSProvider:
        client = make_client()
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(listings.CAPABILITIES | content.CAPABILITIES),
            {"listings": listings.build(client), "content": content.build(client)},
        )

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    use_guesty_adapters(monkeypatch)
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
        assert "administrator" not in tool["description"]
        assert tool["description"].strip()
    assert set(by_name["list_listings"]["inputSchema"]["properties"]) == {
        "search",
        "active_only",
        "limit",
        "cursor",
        "connection",
    }
    for name in ("get_listing", "get_listing_details", "list_listing_photos"):
        assert by_name[name]["inputSchema"]["required"] == ["listing_id"]
    assert not [n for n in by_name if "photo" in n and n != "list_listing_photos"]


async def test_tools_with_a_fake_provider(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    items = [Listing(id=f"l{n}", title=f"House {n}") for n in range(3)]
    use_fake_provider(monkeypatch, items)
    first = await world.call("list_listings", limit=2)
    assert first["isError"] is False
    page = first["structuredContent"]
    assert [item["id"] for item in page["items"]] == ["l0", "l1"] and page["next_cursor"] == "2"
    second = await world.call("list_listings", limit=2, cursor=page["next_cursor"])
    assert [item["id"] for item in second["structuredContent"]["items"]] == ["l2"]
    found = await world.call("list_listings", search="house 1")
    assert [item["id"] for item in found["structuredContent"]["items"]] == ["l1"]
    one = await world.call("get_listing", listing_id="l2")
    assert one["structuredContent"]["title"] == "House 2"
    details = await world.call("get_listing_details", listing_id="known")
    assert details["structuredContent"]["amenities"] == ["Wifi"]
    photos = await world.call("list_listing_photos", listing_id="known")
    assert photos["structuredContent"]["listing_id"] == "known"
    assert photos["structuredContent"]["photos"][0]["caption"] == "Hall"


async def test_errors_are_plain_messages(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_provider(monkeypatch, [])
    missing = await world.call("get_listing", listing_id="nope")
    assert missing["isError"] is True and "not found" in result_text(missing).lower()
    bad_cursor = await world.call("list_listings", cursor="not-a-number")
    assert bad_cursor["isError"] is True and "cursor" in result_text(bad_cursor)
    too_long = await world.call("list_listings", cursor="9" * 1001)
    assert too_long["isError"] is True and "cursor" in result_text(too_long)


async def test_a_provider_without_content_refuses_the_content_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")  # listings only

    monkeypatch.setattr(registry, "load_provider", load)
    for name in ("get_listing_details", "list_listing_photos"):
        refused = await world.call(name, listing_id="x")
        assert refused["isError"] is True and "does not support" in result_text(refused)
    assert (await world.call("list_listings"))["isError"] is False


async def test_guesty_listings_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(LISTINGS_URL).mock(
        return_value=json_response(fixture("listings_envelope.json"))
    )
    result = await world.call("list_listings", limit=2, search="studio")
    assert result["isError"] is False
    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == "2" and params["q"] == "studio" and params["active"] == "true"
    page = result["structuredContent"]
    assert page["total"] == 5 and len(page["items"]) == 2 and page["next_cursor"]
    next_page = await world.call("list_listings", limit=2, cursor=page["next_cursor"])
    assert next_page["isError"] is False
    assert dict(route.calls.last.request.url.params)["skip"] == "2"


async def test_the_page_size_is_kept_within_bounds(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(LISTINGS_URL).mock(return_value=json_response([]))
    await world.call("list_listings", limit=100000)
    assert dict(route.calls.last.request.url.params)["limit"] == "100"
    await world.call("list_listings", limit=0)
    assert dict(route.calls.last.request.url.params)["limit"] == "1"
    await world.call("list_listings", active_only=False)
    assert "active" not in dict(route.calls.last.request.url.params)
    await world.call("list_listings")
    assert dict(route.calls.last.request.url.params)["limit"] == "25"


async def test_channel_data_never_reaches_the_result(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(f"{LISTINGS_URL}/{ID}").mock(
        return_value=json_response(fixture("listing_full.json"))
    )
    result = await world.call("get_listing", listing_id=ID)
    assert result["isError"] is False
    shown = str(result)
    for leaked in ("fakechannel", "fake-channel", "integrations", "accessToken", "cafe"):
        assert leaked not in shown
    assert result["structuredContent"]["title"] == "Harbour View Loft"


async def test_guesty_details_and_photos(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(f"{LISTINGS_URL}/{ID}").mock(
        return_value=json_response(fixture("details_full.json"))
    )
    guesty.get(f"{PHOTOS_URL}/{ID}").mock(return_value=json_response(fixture("photos.json")))
    details = await world.call("get_listing_details", listing_id=ID)
    assert details["isError"] is False
    assert details["structuredContent"]["amenities"] == ["Wifi", "Kitchen", "Heating"]
    photos = await world.call("list_listing_photos", listing_id=ID)
    assert photos["isError"] is False
    shown = photos["structuredContent"]["photos"]
    assert [photo["caption"] for photo in shown][:2] == ["First", None]
    assert len(shown) == 3


async def test_a_guesty_404_is_reported_plainly(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(f"{LISTINGS_URL}/{ID}").mock(
        return_value=json_response({"message": "private upstream text"}, 404)
    )
    result = await world.call("get_listing", listing_id=ID)
    assert result["isError"] is True
    assert result_text(result).endswith("Not found in Guesty.")
    assert "private upstream text" not in result_text(result)


async def test_a_hostile_id_cannot_leave_the_listing_path(
    world: World, guesty: respx.MockRouter
) -> None:
    route = guesty.get(url__regex=r".*").mock(return_value=json_response({"_id": "x"}))
    await world.call("get_listing", listing_id="a/b")
    assert route.calls.last.request.url.raw_path.decode().startswith("/v1/listings/a%2Fb")
    before = route.call_count
    refused = await world.call("get_listing", listing_id="../accounts")
    assert refused["isError"] is True and route.call_count == before
