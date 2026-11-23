"""Who may use the inbox tools: only people with a valid token, on connections it allows.

A connection the token does not allow gets exactly the answer an unknown one gets, no provider is
built for it, and nothing about it appears in any answer. Sending a message and adding a note also
need the connection's change switch.
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
from pmsbridge.pms.models import (
    Conversation,
    ConversationNote,
    ConversationQuery,
    Message,
    MessageDirection,
    OutgoingMessage,
    Page,
    PageRequest,
)
from pmsbridge.pms.testing import FakeProvider

SECRET_LABEL = "Confidential owner portfolio"  # noqa: S105
TOOL_ARGUMENTS: dict[str, dict[str, object]] = {
    "list_conversations": {},
    "get_conversation_messages": {"conversation_id": "c1"},
    "send_guest_message": {"conversation_id": "c1", "body": "Hello"},
    "add_conversation_note": {"conversation_id": "c1", "body": "Note"},
}
MESSAGE = Message(
    id="m1", conversation_id="c1", direction=MessageDirection.INBOUND, body="Private guest words"
)


class FakeInbox:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def list_conversations(self, query: ConversationQuery) -> Page[Conversation]:
        return Page[Conversation](items=[Conversation(id="c1")])

    async def get_conversation_messages(
        self, conversation_id: str, page: PageRequest
    ) -> Page[Message]:
        return Page[Message](items=[MESSAGE])

    async def send_guest_message(self, message: OutgoingMessage) -> Message:
        self.sent.append(message)
        return MESSAGE

    async def add_conversation_note(self, note: ConversationNote) -> Message:
        return MESSAGE


class InboxProvider(FakeProvider):
    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.INBOX_READ, Capability.INBOX_SEND, Capability.INBOX_NOTE})

    @property
    def inbox(self):  # type: ignore[no-untyped-def]
        return FakeInbox()


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
        return InboxProvider([], "x")

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
    for tool in ("list_conversations", "get_conversation_messages"):
        assert (await call(client, token, tool, **TOOL_ARGUMENTS[tool]))["isError"] is False
    loads.clear()
    for tool in ("send_guest_message", "add_conversation_note"):
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
    unclear = await call(
        client, token, "send_guest_message", **TOOL_ARGUMENTS["send_guest_message"]
    )
    assert unclear["isError"] is True and loads == []
    chosen = await call(
        client,
        token,
        "send_guest_message",
        connection="City flats",
        **TOOL_ARGUMENTS["send_guest_message"],
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
    assert "Private guest words" not in response.text


async def test_a_disabled_person_cannot_message_a_guest(
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
        {"name": "send_guest_message", "arguments": TOOL_ARGUMENTS["send_guest_message"]},
    )
    assert response.status_code == 401 and loads == []


async def test_no_token_means_no_inbox(client: httpx.AsyncClient, loads: list[str]) -> None:
    for tool, arguments in TOOL_ARGUMENTS.items():
        response = await rpc(client, None, "tools/call", {"name": tool, "arguments": arguments})
        assert response.status_code == 401
        assert "Private guest words" not in response.text
    assert loads == []
