"""The listing tools through the MCP endpoint: with a fake provider and with the Guesty adapters."""

import json
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

from pmsbridge.mcp import framework
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
    PhotoCaptionUpdate,
    PhotoDelete,
    PhotoOrder,
)
from pmsbridge.pms.testing import FakeProvider

ID = "6a1f00000000000000000001"
TOOLS = ("list_listings", "get_listing", "get_listing_details", "list_listing_photos")
PHOTO_TOOLS = ("update_photo_caption", "reorder_photos", "delete_photo")
PHOTO_ARGUMENTS: dict[str, dict[str, object]] = {
    "update_photo_caption": {"listing_id": "l1", "photo_id": "p1", "caption": "New"},
    "reorder_photos": {"listing_id": "l1", "photo_ids": ["p1", "p2"]},
    "delete_photo": {"listing_id": "l1", "photo_id": "p1"},
}
A1, A2, A3 = (f"6a1f0000000000000000a00{n}" for n in (1, 2, 3))


class FakeContent:
    def __init__(self) -> None:
        self.captions: list[PhotoCaptionUpdate] = []
        self.orders: list[PhotoOrder] = []
        self.deleted: list[PhotoDelete] = []

    async def update_photo_caption(self, update: PhotoCaptionUpdate) -> ListingPhoto:
        self.captions.append(update)
        return ListingPhoto(id=update.photo_id, url="https://images.example.test/1.jpg")

    async def reorder_photos(self, order: PhotoOrder) -> list[ListingPhoto]:
        self.orders.append(order)
        return [
            ListingPhoto(id=photo_id, url=f"https://images.example.test/{photo_id}.jpg")
            for photo_id in order.photo_ids
        ]

    async def delete_photo(self, photo: PhotoDelete) -> None:
        self.deleted.append(photo)

    async def get_listing_details(self, listing_id: str) -> ListingDetails:
        if listing_id != "known":
            raise PMSNotFoundError("Listing not found.")
        return ListingDetails(listing_id="known", amenities=["Wifi"])

    async def list_listing_photos(self, listing_id: str) -> list[ListingPhoto]:
        return [ListingPhoto(id="p1", url="https://images.example.test/1.jpg", caption="Hall")]


class FakeContentProvider(FakeProvider):
    def __init__(self, listings_data: list[Listing], label: str, api: FakeContent) -> None:
        super().__init__(listings_data, label)
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.LISTINGS_READ,
                Capability.LISTING_DETAILS_READ,
                Capability.PHOTOS_READ,
                Capability.PHOTOS_WRITE,
            }
        )

    @property
    def content(self):  # type: ignore[no-untyped-def]
        return self._api


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""
        self.connection_id = ""

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        arguments = {"connection": "Beach houses", **arguments}
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> AsyncIterator[World]:
    built = World(client)
    user = await seed_user()
    built.connection_id = await seed_connection("Beach houses", writes_enabled=True)
    read_only = await seed_connection("Read only", writes_enabled=False)
    built.token, _ = await mint_token(user, connections=(built.connection_id, read_only))
    yield built


def use_fake_provider(
    monkeypatch: pytest.MonkeyPatch,
    listings_data: list[Listing],
    api: FakeContent | None = None,
) -> FakeContent:
    fake = api or FakeContent()

    async def load(connection_id: str) -> PMSProvider:
        return FakeContentProvider(listings_data, "Beach houses", fake)

    monkeypatch.setattr(registry, "load_provider", load)
    return fake


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


async def test_the_photo_changes_are_published_as_gated_changes(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in listed}
    for name in PHOTO_TOOLS:
        tool = by_name[name]
        assert tool["annotations"]["readOnlyHint"] is False
        assert "changes data" in tool["description"]
        assert "connection" in tool["inputSchema"]["properties"]
        text = str(tool).lower()
        assert "guesty" not in text and "hostaway" not in text
    assert by_name["update_photo_caption"]["inputSchema"]["required"] == [
        "listing_id",
        "photo_id",
        "caption",
    ]
    assert by_name["reorder_photos"]["inputSchema"]["required"] == ["listing_id", "photo_ids"]
    assert by_name["delete_photo"]["inputSchema"]["required"] == ["listing_id", "photo_id"]
    # Only these photo tools exist: no upload, no replacing an image.
    assert {n for n in by_name if "photo" in n} == {"list_listing_photos", *PHOTO_TOOLS}
    for name in ("update_photo_caption",):
        assert "url" not in by_name[name]["inputSchema"]["properties"]


async def test_the_descriptions_tell_the_assistant_what_to_do(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    words = {tool["name"]: " ".join(tool["description"].split()) for tool in listed}
    deleting = words["delete_photo"]
    assert "This cannot be undone" in deleting and "explicitly confirmed" in deleting
    assert "read again afterwards to confirm" in deleting
    for name in PHOTO_TOOLS:
        assert "outcome is unknown" in words[name] or "could not be confirmed" in words[name]
        assert "list_listing_photos" in words[name]
    assert "confirmation" in words["update_photo_caption"]
    assert "every photo id" in words["reorder_photos"].lower()


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


# ---- Photo changes ----


async def test_a_caption_change_builds_the_canonical_request(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = use_fake_provider(monkeypatch, [])
    result = await world.call("update_photo_caption", listing_id=" l1 ", photo_id="p1", caption="")
    assert result["isError"] is False, result_text(result)
    assert api.captions == [PhotoCaptionUpdate(listing_id="l1", photo_id="p1", caption="")]
    assert result["structuredContent"]["id"] == "p1"


async def test_a_reorder_builds_the_canonical_request_and_returns_the_photos(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = use_fake_provider(monkeypatch, [])
    result = await world.call("reorder_photos", listing_id="l1", photo_ids=["p2", "p1"])
    assert result["isError"] is False, result_text(result)
    assert api.orders == [PhotoOrder(listing_id="l1", photo_ids=["p2", "p1"])]
    shown = result["structuredContent"]
    assert shown["listing_id"] == "l1" and [p["id"] for p in shown["photos"]] == ["p2", "p1"]


async def test_a_delete_reports_the_photo_as_gone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = use_fake_provider(monkeypatch, [])
    result = await world.call("delete_photo", listing_id="l1", photo_id="p1")
    assert result["isError"] is False, result_text(result)
    assert api.deleted == [PhotoDelete(listing_id="l1", photo_id="p1")]
    assert result["structuredContent"] == {"listing_id": "l1", "photo_id": "p1", "deleted": True}


async def test_bad_photo_input_is_refused_plainly_before_the_provider_is_called(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = use_fake_provider(monkeypatch, [])
    long = "caption words " * 60
    cases = (
        ("update_photo_caption", {"listing_id": "l1", "photo_id": "p1", "caption": long}),
        ("update_photo_caption", {"listing_id": " ", "photo_id": "p1", "caption": "x"}),
        ("update_photo_caption", {"listing_id": "l1", "photo_id": "", "caption": "x"}),
        ("reorder_photos", {"listing_id": "l1", "photo_ids": ["p1", "p1"]}),
        ("reorder_photos", {"listing_id": "l1", "photo_ids": []}),
        ("reorder_photos", {"listing_id": "l1", "photo_ids": [f"p{n}" for n in range(201)]}),
        ("delete_photo", {"listing_id": "", "photo_id": "p1"}),
        ("delete_photo", {"listing_id": "l1", "photo_id": " "}),
    )
    for name, arguments in cases:
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True, (name, arguments)
        assert "caption words" not in result_text(refused)
        assert "Traceback" not in result_text(refused)
    assert api.captions == [] and api.orders == [] and api.deleted == []


@pytest.mark.parametrize("tool", PHOTO_TOOLS)
async def test_a_photo_change_is_refused_where_changes_are_not_allowed(
    world: World, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    api = use_fake_provider(monkeypatch, [])
    refused = await world.call(tool, connection="Read only", **PHOTO_ARGUMENTS[tool])
    assert refused["isError"] is True
    assert "Changes are not allowed on the connection" in result_text(refused)
    assert api.captions == [] and api.orders == [] and api.deleted == []
    reading = await world.call("list_listing_photos", connection="Read only", listing_id="l1")
    assert reading["isError"] is False  # reading still works on a read-only connection


async def test_photo_changes_have_their_own_limit_for_each_approval(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = use_fake_provider(monkeypatch, [])
    for _ in range(framework.WRITES_BURST):
        done = await world.call("delete_photo", **PHOTO_ARGUMENTS["delete_photo"])
        assert done["isError"] is False
    refused = await world.call("delete_photo", **PHOTO_ARGUMENTS["delete_photo"])
    assert refused["isError"] is True and "changes too quickly" in result_text(refused)
    assert len(api.deleted) == framework.WRITES_BURST


async def test_a_provider_without_photo_changes_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    for name in PHOTO_TOOLS:
        refused = await world.call(name, **PHOTO_ARGUMENTS[name])
        assert refused["isError"] is True and "does not support" in result_text(refused)


def photo_list(guesty: respx.MockRouter, *answers: object) -> respx.Route:
    queue = list(answers or (fixture("photos_three.json"),))

    def answer(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item if isinstance(item, httpx.Response) else json_response(item)

    return guesty.get(f"{PHOTOS_URL}/{ID}").mock(side_effect=answer)


async def test_guesty_caption_end_to_end_is_one_post(
    world: World, guesty: respx.MockRouter
) -> None:
    photo_list(guesty)
    change = guesty.post(f"{PHOTOS_URL}/{ID}/{A2}").mock(
        return_value=json_response(fixture("photos_captioned.json"), 201)
    )
    result = await world.call(
        "update_photo_caption", listing_id=ID, photo_id=A2, caption="Kitchen view"
    )
    assert result["isError"] is False, result_text(result)
    assert json.loads(change.calls.last.request.content) == {"caption": "Kitchen view"}
    assert change.call_count == 1 and result["structuredContent"]["caption"] == "Kitchen view"


async def test_guesty_reorder_end_to_end_is_one_post(
    world: World, guesty: respx.MockRouter
) -> None:
    photo_list(guesty)
    change = guesty.post(f"{PHOTOS_URL}/{ID}/order").mock(
        return_value=json_response(fixture("photos_reordered.json"), 201)
    )
    result = await world.call("reorder_photos", listing_id=ID, photo_ids=[A3, A1, A2])
    assert result["isError"] is False, result_text(result)
    assert json.loads(change.calls.last.request.content) == {"order": [A3, A1, A2]}
    assert [p["id"] for p in result["structuredContent"]["photos"]] == [A3, A1, A2]


async def test_guesty_reorder_with_an_unknown_photo_changes_nothing(
    world: World, guesty: respx.MockRouter
) -> None:
    photo_list(guesty)
    change = guesty.post(f"{PHOTOS_URL}/{ID}/order").mock(return_value=json_response([], 201))
    result = await world.call("reorder_photos", listing_id=ID, photo_ids=[A3, A1, "nope"])
    assert result["isError"] is True and "nothing was changed" in result_text(result)
    assert change.call_count == 0


async def test_guesty_delete_end_to_end_is_confirmed_by_a_second_read(
    world: World, guesty: respx.MockRouter
) -> None:
    reads = photo_list(guesty, fixture("photos_three.json"), fixture("photos_after_delete.json"))
    removal = guesty.delete(f"{PHOTOS_URL}/{ID}/{A2}").mock(return_value=httpx.Response(204))
    result = await world.call("delete_photo", listing_id=ID, photo_id=A2)
    assert result["isError"] is False, result_text(result)
    assert result["structuredContent"] == {"listing_id": ID, "photo_id": A2, "deleted": True}
    assert removal.call_count == 1 and reads.call_count == 2


async def test_guesty_delete_of_a_photo_that_stays_is_an_error_not_a_success(
    world: World, guesty: respx.MockRouter
) -> None:
    photo_list(guesty)
    removal = guesty.delete(f"{PHOTOS_URL}/{ID}/{A2}").mock(return_value=httpx.Response(200))
    result = await world.call("delete_photo", listing_id=ID, photo_id=A2)
    assert result["isError"] is True and "still in the list" in result_text(result)
    assert removal.call_count == 1


async def test_guesty_timeout_reports_an_unknown_outcome_and_is_not_resent(
    world: World, guesty: respx.MockRouter
) -> None:
    photo_list(guesty)
    removal = guesty.delete(f"{PHOTOS_URL}/{ID}/{A2}").mock(side_effect=httpx.ReadTimeout("slow"))
    result = await world.call("delete_photo", listing_id=ID, photo_id=A2)
    assert result["isError"] is True
    assert "outcome is unknown" in result_text(result) and removal.call_count == 1


async def test_guesty_photo_failures_do_not_leak_upstream_text_or_the_caption(
    world: World, guesty: respx.MockRouter
) -> None:
    photo_list(guesty)
    guesty.post(f"{PHOTOS_URL}/{ID}/{A2}").mock(
        return_value=json_response({"message": "private upstream"}, 400)
    )
    result = await world.call(
        "update_photo_caption", listing_id=ID, photo_id=A2, caption="secret caption words"
    )
    assert result["isError"] is True
    assert "private upstream" not in result_text(result)
    assert "secret caption" not in result_text(result)
