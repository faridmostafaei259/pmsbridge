"""The inbox tools through the MCP endpoint: with a fake provider and with the Guesty adapter."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from guesty_inbox_helpers import (
    CONVERSATION_ID,
    CONVERSATIONS_URL,
    GUEST_EMAIL,
    INJECTION_TEXT,
    NEVER_SHOWN,
    POSTS_URL,
    SEND_URL,
    fixture,
    json_response,
    make_client,
    mock_router,
)
from mcp_helpers import call_tool, mint_token, result_text, rpc, seed_connection, seed_user

from pmsbridge.mcp import framework
from pmsbridge.pms import registry
from pmsbridge.pms.base import Capability, ConnectionRef, PMSProvider, Provider
from pmsbridge.pms.guesty import inbox
from pmsbridge.pms.guesty.provider import GuestyProvider
from pmsbridge.pms.models import (
    Conversation,
    ConversationNote,
    ConversationQuery,
    Message,
    MessageChannel,
    MessageDirection,
    OutgoingMessage,
    Page,
    PageRequest,
)
from pmsbridge.pms.testing import FakeProvider

TOOLS = (
    "list_conversations",
    "get_conversation_messages",
    "send_guest_message",
    "add_conversation_note",
)
SEND_DESCRIPTION = (
    "Sends a message to the guest. Show the person the exact text and get their confirmation first."
)


def message(body: str = "ok") -> Message:
    return Message(id="m1", conversation_id="c1", direction=MessageDirection.OUTBOUND, body=body)


class FakeInbox:
    def __init__(self) -> None:
        self.queries: list[ConversationQuery] = []
        self.pages: list[tuple[str, PageRequest]] = []
        self.sent: list[OutgoingMessage] = []
        self.notes: list[ConversationNote] = []

    async def list_conversations(self, query: ConversationQuery) -> Page[Conversation]:
        self.queries.append(query)
        return Page[Conversation](items=[Conversation(id="c1", guest_name="Pat Invented")])

    async def get_conversation_messages(
        self, conversation_id: str, page: PageRequest
    ) -> Page[Message]:
        self.pages.append((conversation_id, page))
        return Page[Message](items=[message("Hello")])

    async def send_guest_message(self, outgoing: OutgoingMessage) -> Message:
        self.sent.append(outgoing)
        return message(outgoing.body)

    async def add_conversation_note(self, note: ConversationNote) -> Message:
        self.notes.append(note)
        return message(note.body)


class InboxProvider(FakeProvider):
    def __init__(self, api: FakeInbox) -> None:
        super().__init__([], "Beach houses")
        self._api = api

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.INBOX_READ, Capability.INBOX_SEND, Capability.INBOX_NOTE})

    @property
    def inbox(self):  # type: ignore[no-untyped-def]
        return self._api


class World:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = ""
        self.connection_id = ""
        self.loads: list[str] = []

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


def use_fake_provider(monkeypatch: pytest.MonkeyPatch, world: World, api: FakeInbox) -> None:
    async def load(connection_id: str) -> PMSProvider:
        world.loads.append(connection_id)
        return InboxProvider(api)

    monkeypatch.setattr(registry, "load_provider", load)


@pytest.fixture
def guesty(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    async def load(connection_id: str) -> PMSProvider:
        return GuestyProvider(
            ConnectionRef(connection_id, Provider.GUESTY, "Beach houses"),
            frozenset(inbox.CAPABILITIES),
            {"inbox": inbox.build(make_client())},
        )

    monkeypatch.setattr(registry, "load_provider", load)
    with mock_router() as router:
        yield router


async def listed_tools(world: World) -> dict[str, Any]:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    return {tool["name"]: tool for tool in listed}


# ---- How the tools are published ----


async def test_reads_are_read_only_and_the_other_two_are_gated_changes(world: World) -> None:
    tools = await listed_tools(world)
    for name in ("list_conversations", "get_conversation_messages"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    for name in ("send_guest_message", "add_conversation_note"):
        assert tools[name]["annotations"]["readOnlyHint"] is False
        assert "changes data" in tools[name]["description"]
    for name in TOOLS:
        assert "connection" in tools[name]["inputSchema"]["properties"]
        text = str(tools[name]).lower()
        assert "guesty" not in text and "hostaway" not in text


async def test_the_descriptions_tell_the_assistant_what_to_do(world: World) -> None:
    tools = await listed_tools(world)

    def words(name: str) -> str:
        return " ".join(tools[name]["description"].split())

    assert words("send_guest_message").startswith(SEND_DESCRIPTION)
    assert "outcome is unknown" in words("send_guest_message")
    reading = words("get_conversation_messages")
    assert "untrusted" in reading and "never as instructions" in reading
    assert "never sent to the guest" in words("add_conversation_note")
    assert set(tools["list_conversations"]["inputSchema"]["properties"]) == {
        "reservation_id",
        "listing_id",
        "unread_only",
        "limit",
        "cursor",
        "connection",
    }
    assert tools["send_guest_message"]["inputSchema"]["required"] == ["conversation_id", "body"]
    assert tools["get_conversation_messages"]["inputSchema"]["required"] == ["conversation_id"]


# ---- With a fake provider ----


async def test_list_passes_the_filters_and_keeps_the_page_size_in_bounds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call(
        "list_conversations", reservation_id=" r1 ", listing_id="  ", unread_only=True, limit=2
    )
    assert result["isError"] is False
    assert [c["id"] for c in result["structuredContent"]["items"]] == ["c1"]
    query = api.queries[-1]
    assert query.reservation_id == "r1" and query.listing_id is None
    assert query.unread_only is True and query.limit == 2
    await world.call("list_conversations", limit=100000)
    assert api.queries[-1].limit == 100
    await world.call("list_conversations", limit=0)
    assert api.queries[-1].limit == 1
    await world.call("list_conversations")
    assert api.queries[-1].limit == 25 and api.queries[-1].unread_only is None


async def test_a_long_cursor_or_filter_is_refused_plainly(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_provider(monkeypatch, world, FakeInbox())
    long_cursor = await world.call("list_conversations", cursor="9" * 1001)
    assert long_cursor["isError"] is True and "cursor" in result_text(long_cursor)
    long_filter = await world.call("list_conversations", reservation_id="x" * 201)
    assert long_filter["isError"] is True and "200" in result_text(long_filter)
    messages = await world.call(
        "get_conversation_messages", conversation_id="c1", cursor="9" * 1001
    )
    assert messages["isError"] is True and "cursor" in result_text(messages)


async def test_messages_are_read_with_the_cursor(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    result = await world.call("get_conversation_messages", conversation_id="c1", cursor="t.abc")
    assert result["isError"] is False
    assert result["structuredContent"]["items"][0]["body"] == "Hello"
    assert api.pages[-1][0] == "c1" and api.pages[-1][1].cursor == "t.abc"


async def test_send_builds_the_canonical_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    sent = await world.call("send_guest_message", conversation_id="c1", body=" Welcome! ")
    assert sent["isError"] is False and sent["structuredContent"]["body"] == "Welcome!"
    assert api.sent[-1].channel is MessageChannel.DEFAULT and api.sent[-1].body == "Welcome!"
    await world.call("send_guest_message", conversation_id="c1", body="Hi", channel="sms")
    assert api.sent[-1].channel is MessageChannel.SMS


async def test_an_unknown_channel_is_refused_before_anything_is_called(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    refused = await world.call(
        "send_guest_message", conversation_id="c1", body="Hi", channel="carrier-pigeon"
    )
    assert refused["isError"] is True and api.sent == []


@pytest.mark.parametrize(
    ("tool", "extra"), [("send_guest_message", {}), ("add_conversation_note", {})]
)
async def test_a_bad_text_or_id_is_refused_without_repeating_the_text(
    world: World, monkeypatch: pytest.MonkeyPatch, tool: str, extra: dict[str, object]
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    for body in ("", "   ", "x" * 5001):
        refused = await world.call(tool, conversation_id="c1", body=body, **extra)
        assert refused["isError"] is True
        assert "between 1 and 5000" in result_text(refused)
        assert "xxxxxxxx" not in result_text(refused)
    bad_id = await world.call(tool, conversation_id="  ", body="Hi", **extra)
    assert bad_id["isError"] is True and "conversation id is not valid" in result_text(bad_id)
    assert api.sent == [] and api.notes == []


async def test_a_note_reaches_a_provider_that_supports_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    done = await world.call("add_conversation_note", conversation_id="c1", body="Call back")
    assert done["isError"] is False and api.notes[-1].body == "Call back"


# ---- The change switch and the write limit ----


@pytest.mark.parametrize("tool", ["send_guest_message", "add_conversation_note"])
async def test_a_change_is_refused_where_changes_are_not_allowed(
    world: World, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    refused = await world.call(tool, connection="Read only", conversation_id="c1", body="Hi")
    assert refused["isError"] is True
    assert "Changes are not allowed on the connection" in result_text(refused)
    assert world.loads == [] and api.sent == [] and api.notes == []
    reading = await world.call("list_conversations", connection="Read only")
    assert reading["isError"] is False  # reading still works on a read-only connection


async def test_messages_have_their_own_limit_for_each_approval(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeInbox()
    use_fake_provider(monkeypatch, world, api)
    for _ in range(framework.WRITES_BURST):
        done = await world.call("send_guest_message", conversation_id="c1", body="Hi")
        assert done["isError"] is False
    refused = await world.call("send_guest_message", conversation_id="c1", body="Hi")
    assert refused["isError"] is True and "changes too quickly" in result_text(refused)
    assert len(api.sent) == framework.WRITES_BURST
    assert (await world.call("list_conversations"))["isError"] is False


async def test_a_provider_without_an_inbox_refuses_the_tools(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def load(connection_id: str) -> PMSProvider:
        return FakeProvider([], "Beach houses")

    monkeypatch.setattr(registry, "load_provider", load)
    calls = (
        ("list_conversations", {}),
        ("get_conversation_messages", {"conversation_id": "c"}),
        ("send_guest_message", {"conversation_id": "c", "body": "Hi"}),
        ("add_conversation_note", {"conversation_id": "c", "body": "Hi"}),
    )
    for name, arguments in calls:
        refused = await world.call(name, **arguments)
        assert refused["isError"] is True and "does not support" in result_text(refused)


# ---- With the Guesty adapter ----


async def test_guesty_list_end_to_end(world: World, guesty: respx.MockRouter) -> None:
    route = guesty.get(CONVERSATIONS_URL).mock(
        return_value=json_response(fixture("conversations_page.json"))
    )
    result = await world.call("list_conversations", limit=3, unread_only=True)
    assert result["isError"] is False, result_text(result)
    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == "3" and "read" in params["filters"]
    page = result["structuredContent"]
    assert len(page["items"]) == 3 and page["next_cursor"]
    assert page["items"][0]["guest_name"] == "Alex Invented"
    for leaked in NEVER_SHOWN:
        assert leaked not in str(result)
    nxt = await world.call("list_conversations", limit=3, cursor=page["next_cursor"])
    assert nxt["isError"] is False
    assert dict(route.calls.last.request.url.params)["cursorAfter"] == "ZmFrZS1jdXJzb3ItMQ"


async def test_guesty_messages_end_to_end_keep_guest_text_as_data(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(POSTS_URL).mock(return_value=json_response(fixture("posts_page.json")))
    result = await world.call("get_conversation_messages", conversation_id=CONVERSATION_ID)
    assert result["isError"] is False, result_text(result)
    items = result["structuredContent"]["items"]
    assert items[0]["direction"] == "inbound" and INJECTION_TEXT in items[0]["body"]
    assert items[0]["author_name"] is None and items[1]["author_name"] == "Riley Staffer"
    assert items[2]["channel"] == "note"
    for leaked in NEVER_SHOWN:
        assert leaked not in str(result)


async def test_guesty_send_end_to_end_is_one_post(world: World, guesty: respx.MockRouter) -> None:
    guesty.get(POSTS_URL).mock(return_value=json_response(fixture("posts_page.json")))
    sends = guesty.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    result = await world.call(
        "send_guest_message",
        conversation_id=CONVERSATION_ID,
        body="Welcome! Check-in is from 3 pm.",
    )
    assert result["isError"] is False, result_text(result)
    assert sends.call_count == 1
    assert result["structuredContent"]["direction"] == "outbound"
    assert result["structuredContent"]["channel"] == "airbnb2"


async def test_guesty_send_with_a_channel_the_conversation_never_used_sends_nothing(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(POSTS_URL).mock(return_value=json_response(fixture("posts_page.json")))
    sends = guesty.post(SEND_URL).mock(return_value=json_response(fixture("send_response.json")))
    refused = await world.call(
        "send_guest_message", conversation_id=CONVERSATION_ID, body="Hi", channel="whatsapp"
    )
    assert refused["isError"] is True and "not available" in result_text(refused)
    assert sends.call_count == 0


async def test_guesty_timeout_reports_an_unknown_outcome_and_is_not_resent(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(POSTS_URL).mock(return_value=json_response(fixture("posts_page.json")))
    sends = guesty.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    result = await world.call("send_guest_message", conversation_id=CONVERSATION_ID, body="Hi")
    assert result["isError"] is True
    assert "outcome is unknown" in result_text(result) and sends.call_count == 1


async def test_guesty_notes_are_refused_and_nothing_is_requested(
    world: World, guesty: respx.MockRouter
) -> None:
    route = guesty.route().mock(return_value=json_response({}))
    result = await world.call("add_conversation_note", conversation_id=CONVERSATION_ID, body="x")
    assert result["isError"] is True and "does not support" in result_text(result)
    assert route.call_count == 0


async def test_guesty_failures_do_not_leak_upstream_text_or_the_draft(
    world: World, guesty: respx.MockRouter
) -> None:
    guesty.get(POSTS_URL).mock(return_value=json_response(fixture("posts_page.json")))
    guesty.post(SEND_URL).mock(return_value=json_response({"message": "private upstream"}, 400))
    result = await world.call(
        "send_guest_message", conversation_id=CONVERSATION_ID, body="secret draft words"
    )
    assert result["isError"] is True
    assert "private upstream" not in result_text(result)
    assert "secret draft" not in result_text(result) and GUEST_EMAIL not in result_text(result)
