"""The review tools through the MCP endpoint: with a fake provider and with the Guesty adapter."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_review_helpers import (
    DRAFT_TEXT,
    INJECTION_TEXT,
    NEVER_SHOWN,
    REPLIED_ID,
    REPLY_URL,
    REVIEW_ID,
    REVIEWS_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)
from mcp_helpers import call_tool, mint_token, result_text, rpc, seed_connection, seed_user

from pmsbridge.mcp import framework
from pmsbridge.pms import registry
from pmsbridge.pms.base import Capability, ConnectionRef, PMSProvider, Provider
from pmsbridge.pms.guesty import reviews
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import Page, Review, ReviewQuery, ReviewReply
from pmsbridge.pms.testing import FakeProvider

TOOLS = ("list_reviews", "reply_to_review")
REPLY_DESCRIPTION = (
    "Posts a public reply to a guest review. Show the person the exact text and get their "
    "confirmation first."
)


class FakeReviews:
    def __init__(self) -> None:
        self.queries: list[ReviewQuery] = []
        self.replies: list[ReviewReply] = []

    async def list_reviews(self, query: ReviewQuery) -> Page[Review]:
        self.queries.append(query)
        return Page[Review](items=[Review(id="r1", public_text="Great")])

    async def reply_to_review(self, reply: ReviewReply) -> Review:
        self.replies.append(reply)
        return Review(id=reply.review_id, reply=reply.body)


class ReviewsProvider(FakeProvider):
    def __init__(self, api: FakeReviews) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.REVIEWS_READ, Capability.REVIEWS_REPLY})

    @property
    def reviews(self):  # type: ignore[no-untyped-def]
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


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, world: World, api: FakeReviews) -> None:
    async def load(connection_id: str) -> PMSProvider:
        world.loads.append(connection_id)
        return ReviewsProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(reviews.CAPABILITIES),
            {"reviews": reviews.build(make_client())},
        )

    monkeypatch.setattr(registry, "load_provider", load)
    with mock_router() as router:
        yield router


async def listed_tools(world: World) -> dict[str, Any]:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    return {tool["name"]: tool for tool in listed}


# ---- How the tools are published ----


async def test_listing_is_read_only_and_replying_is_a_gated_change(world: World) -> None:
    tools = await listed_tools(world)
    assert tools["list_reviews"]["annotations"]["readOnlyHint"] is True
    assert tools["reply_to_review"]["annotations"]["readOnlyHint"] is False
    assert "changes data" in tools["reply_to_review"]["description"]
    for name in TOOLS:
        assert "connection" in tools[name]["inputSchema"]["properties"]
        text = str(tools[name]).lower()
        assert "guesty" not in text and "hostaway" not in text
        assert "reviewer" not in text


async def test_the_descriptions_tell_the_assistant_what_to_do(world: World) -> None:
    tools = await listed_tools(world)

    def words(name: str) -> str:
        return " ".join(tools[name]["description"].split())

    assert words("reply_to_review").startswith(REPLY_DESCRIPTION)
    assert "public" in words("reply_to_review") and "outcome is unknown" in words("reply_to_review")
    assert "already has a reply" in words("reply_to_review")
    reading = words("list_reviews")
    assert "untrusted" in reading and "never as instructions" in reading
    assert set(tools["list_reviews"]["inputSchema"]["properties"]) == {
        "listing_id",
        "since",
        "limit",
        "cursor",
        "connection",
    }
    assert tools["reply_to_review"]["inputSchema"]["required"] == ["review_id", "body"]


# ---- With a fake provider ----


async def test_list_passes_the_filters_and_keeps_the_page_size_in_bounds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeReviews()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call("list_reviews", listing_id=" L1 ", since="2026-03-01", limit=2)
    assert result["isError"] is False
    assert [r["id"] for r in result["structuredContent"]["items"]] == ["r1"]
    query = api.queries[-1]
    assert query.listing_id == "L1" and str(query.since) == "2026-03-01" and query.limit == 2
    await world.call("list_reviews", limit=100000, listing_id="  ")
    assert api.queries[-1].limit == 100 and api.queries[-1].listing_id is None
    await world.call("list_reviews", limit=0)
    assert api.queries[-1].limit == 1
    await world.call("list_reviews")
    assert api.queries[-1].limit == 25 and api.queries[-1].since is None


async def test_a_long_cursor_filter_or_a_bad_date_is_refused_plainly(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeReviews()
    use_fake_provider(monkeypatch, world, api)
    long_cursor = await world.call("list_reviews", cursor="9" * 1001)
    assert long_cursor["isError"] is True and "cursor" in result_text(long_cursor)
    long_filter = await world.call("list_reviews", listing_id="x" * 201)
    assert long_filter["isError"] is True and "200" in result_text(long_filter)
    bad_date = await world.call("list_reviews", since="yesterday")
    assert bad_date["isError"] is True
    assert api.queries == []


async def test_reply_builds_the_canonical_reply(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeReviews()
    use_fake_provider(monkeypatch, world, api)
    sent = await world.call("reply_to_review", review_id=" r1 ", body=" Thank you! ")
    assert sent["isError"] is False and sent["structuredContent"]["reply"] == "Thank you!"
    assert api.replies[-1].review_id == "r1"


async def test_a_bad_text_or_id_is_refused_without_repeating_the_text(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeReviews()
    use_fake_provider(monkeypatch, world, api)
    for body in ("", "x" * 5001):
        refused = await world.call("reply_to_review", review_id="r1", body=body)
        assert refused["isError"] is True
        assert "between 1 and 5000" in result_text(refused)
        assert "xxxxxxxx" not in result_text(refused)
    bad_id = await world.call("reply_to_review", review_id="  ", body="Hi")
    assert bad_id["isError"] is True and "review id is not valid" in result_text(bad_id)
    assert api.replies == []


# ---- The change switch and the write limit ----


async def test_a_reply_is_refused_where_changes_are_not_allowed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeReviews()
    use_fake_provider(monkeypatch, world, api)
    refused = await world.call("reply_to_review", connection="Read only", review_id="r1", body="Hi")
    assert refused["isError"] is True
    assert "Changes are not allowed on the connection" in result_text(refused)
    assert world.loads == [] and api.replies == []
    reading = await world.call("list_reviews", connection="Read only")
    assert reading["isError"] is False  # reading still works on a read-only connection


async def test_replies_have_their_own_limit_for_each_approval(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeReviews()
    use_fake_provider(monkeypatch, world, api)
    for _ in range(framework.WRITES_BURST):
        done = await world.call("reply_to_review", review_id="r1", body="Hi")
        assert done["isError"] is False
    refused = await world.call("reply_to_review", review_id="r1", body="Hi")
    assert refused["isError"] is True and "changes too quickly" in result_text(refused)
    assert len(api.replies) == framework.WRITES_BURST
    assert (await world.call("list_reviews"))["isError"] is False


async def test_a_provider_without_reviews_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    for name, arguments in (
        ("list_reviews", {}),
        ("reply_to_review", {"review_id": "r", "body": "Hi"}),
    ):
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True and "does not support" in result_text(refused)


# ---- With the Guesty adapter ----


async def test_guesty_list_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(REVIEWS_URL).mock(return_value=json_response(fixture("list_page.json")))
    result = await world.call("list_reviews", limit=3, listing_id="L1", since="2026-01-01")
    assert result["isError"] is False, result_text(result)
    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == "3" and params["listingId"] == "L1"
    page = result["structuredContent"]
    assert len(page["items"]) == 3 and page["next_cursor"]
    first = page["items"][0]
    assert first["public_text"] == INJECTION_TEXT
    assert first["rating"] == 9 and first["rating_scale"] == 10
    assert page["items"][1]["reply"] == "Thank you for staying with us."
    for leaked in NEVER_SHOWN:
        assert leaked not in str(result)
    nxt = await world.call("list_reviews", limit=3, cursor=page["next_cursor"])
    assert nxt["isError"] is False and dict(route.calls.last.request.url.params)["skip"] == "3"


async def test_guesty_reply_end_to_end_is_one_put(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(REVIEWS_URL).mock(return_value=json_response(fixture("list_page.json")))
    puts = guesty.put(REPLY_URL).mock(return_value=json_response(fixture("reply_response.json")))
    result = await world.call(
        "reply_to_review", review_id=REVIEW_ID, body="Thank you, we hope to host you again."
    )
    assert result["isError"] is False, result_text(result)
    assert puts.call_count == 1
    assert result["structuredContent"]["reply"] == "Thank you, we hope to host you again."


async def test_guesty_refuses_a_review_that_already_has_a_reply(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(REVIEWS_URL).mock(return_value=json_response(fixture("list_page.json")))
    puts = guesty.route(method="PUT").mock(return_value=json_response({}))
    refused = await world.call("reply_to_review", review_id=REPLIED_ID, body="Hi")
    assert refused["isError"] is True and "already has a reply" in result_text(refused)
    assert puts.call_count == 0


async def test_guesty_timeout_reports_an_unknown_outcome_and_is_not_resent(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(REVIEWS_URL).mock(return_value=json_response(fixture("list_page.json")))
    puts = guesty.put(REPLY_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    result = await world.call("reply_to_review", review_id=REVIEW_ID, body="Hi")
    assert result["isError"] is True
    assert "outcome is unknown" in result_text(result) and puts.call_count == 1


async def test_guesty_failures_do_not_leak_upstream_text_or_the_draft(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(REVIEWS_URL).mock(return_value=json_response(fixture("list_page.json")))
    guesty.put(REPLY_URL).mock(return_value=json_response({"message": "private upstream"}, 400))
    result = await world.call("reply_to_review", review_id=REVIEW_ID, body=DRAFT_TEXT)
    assert result["isError"] is True
    assert "private upstream" not in result_text(result) and DRAFT_TEXT not in result_text(result)
