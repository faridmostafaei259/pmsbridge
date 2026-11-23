"""Who may use the MCP endpoint, and which connections each assistant can reach.

The person and the connections come only from the verified token. A connection the token does not
allow gets exactly the answer an unknown one gets, and nothing about it appears in any answer.
"""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select, update

from pmsbridge.accounts import passwords
from pmsbridge.accounts.db import User
from pmsbridge.accounts.service import create_user
from pmsbridge.accounts.sessions import hash_token
from pmsbridge.app import create_app
from pmsbridge.core.audit import AuditLog
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import reset_database, session, write_transaction
from pmsbridge.mcp.framework import ToolCall, pms_tool
from pmsbridge.oauth.config import mcp_resource
from pmsbridge.oauth.db import OAuthClient, OAuthGrant, OAuthToken
from pmsbridge.pms import registry
from pmsbridge.pms.base import (
    Capability,
    ConnectionRef,
    PMSProvider,
    Provider,
    ReservationsAPI,
)
from pmsbridge.pms.db import PMSConnection, sealed_credential_columns
from pmsbridge.pms.testing import FakeProvider

BASE_URL = "https://pms.example.test"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
SECRET_LABEL = "Confidential owner portfolio"  # noqa: S105


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("test"))
    clear_settings_cache()
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(passwords, "_dummy_hash", None)


class WritingProvider(FakeProvider):
    """A fake provider that supports one write capability."""

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.LISTINGS_READ, Capability.RESERVATIONS_ADD_NOTE})

    @property
    def reservations(self) -> ReservationsAPI | None:
        return object()  # type: ignore[return-value]  # a stand-in; the tools never call it


class Loads:
    """Which connections a fake provider was built for."""

    def __init__(self) -> None:
        self.ids: list[str] = []


@pytest.fixture
async def app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    application = create_app(("pmsbridge.core.web", "pmsbridge.oauth.web", "pmsbridge.mcp.server"))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan() -> None:
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    await started.wait()

    loads = Loads()
    application.state.loads = loads

    async def load(connection_id: str) -> PMSProvider:
        loads.ids.append(connection_id)
        provider = WritingProvider([], "x")
        provider._connection = ConnectionRef(connection_id, Provider.GUESTY, "x")
        return provider

    monkeypatch.setattr(registry, "load_provider", load)

    mcp = application.state.mcp_server

    @pms_tool(mcp, name="t_read", capability=Capability.LISTINGS_READ, api=lambda p: p.listings)
    async def t_read(call: ToolCall[Any]) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Read something."""
        return {"connection_id": call.connection.connection_id}

    @pms_tool(
        mcp,
        name="t_write",
        capability=Capability.RESERVATIONS_ADD_NOTE,
        api=lambda p: p.reservations,
    )
    async def t_write(call: ToolCall[Any]) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Change something."""
        return {"connection_id": call.connection.connection_id}

    yield application
    stop.set()
    await task
    await reset_database()


@pytest.fixture
def loads(app: FastAPI) -> Loads:
    loads: Loads = app.state.loads
    return loads


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app, client=("198.51.100.7", 1))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as opened:
        yield opened


async def person(email: str) -> int:
    return await create_user(email, "fake-passphrase-for-tests")


async def connection(label: str, *, status: str = "active", writes: bool = False) -> str:
    identifier = secrets.token_hex(16)
    now = datetime.now(UTC)
    async with write_transaction() as db:
        db.add(
            PMSConnection(
                id=identifier,
                provider="guesty",
                label=label,
                status=status,
                writes_enabled=writes,
                guesty_account_id=f"acct-{identifier[:8]}",
                created_at=now,
                updated_at=now,
                **sealed_credential_columns(
                    identifier, {"client_id": f"k-{identifier}", "client_secret": "s"}
                ),
            )
        )
    return identifier


async def token_for(
    user_id: int, *, allowed: tuple[str, ...] = (), everything: bool = False
) -> tuple[str, int]:
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    client_key = f"assistant-{secrets.token_hex(4)}"
    async with write_transaction() as db:
        db.add(
            OAuthClient(
                client_id=client_key,
                client_name="Test assistant",
                redirect_uris=["https://assistant.example.test/callback"],
                token_endpoint_auth_method="none",  # noqa: S106
                grant_types=["authorization_code"],
                created_at=now,
            )
        )
        grant = OAuthGrant(
            user_id=user_id,
            client_id=client_key,
            all_connections=everything,
            allowed_connection_ids=list(allowed),
            scope="mcp",
            resource=mcp_resource(),
            created_at=now,
        )
        db.add(grant)
        await db.flush()
        db.add(
            OAuthToken(
                token_hash=hash_token(token),
                kind="access",
                grant_id=grant.id,
                client_id=client_key,
                user_id=user_id,
                resource=mcp_resource(),
                scope="mcp",
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        return token, grant.id


async def rpc(
    client: httpx.AsyncClient,
    token: str | None,
    method: str,
    params: dict[str, object] | None = None,
) -> httpx.Response:
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await client.post("/mcp", content=json.dumps(body), headers=headers)


async def call(
    client: httpx.AsyncClient, token: str, tool: str, **arguments: object
) -> dict[str, Any]:
    response = await rpc(client, token, "tools/call", {"name": tool, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]  # type: ignore[no-any-return]


def text_of(result: dict[str, Any]) -> str:
    return " ".join(item["text"] for item in result["content"])


async def audit_rows() -> list[AuditLog]:
    async with session() as db:
        return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


@pytest.mark.parametrize(
    "method", ["initialize", "tools/list", "tools/call", "ping", "resources/list", "prompts/list"]
)
async def test_nothing_is_reachable_without_a_token(client: httpx.AsyncClient, method: str) -> None:
    for token in (None, "not-a-token", "A" * 43):
        response = await rpc(client, token, method, {"name": "list_connections", "arguments": {}})
        assert response.status_code == 401, (method, token)
        assert "list_connections" not in response.text
    assert await audit_rows() == []  # nobody was identified, so nothing is attributed to anyone


async def test_a_token_only_reaches_the_connections_its_grant_allows(
    client: httpx.AsyncClient, loads: Loads
) -> None:
    ada = await person("ada@example.test")
    mine = await connection("Beach houses", writes=True)
    secret = await connection(SECRET_LABEL, writes=True)
    token, _ = await token_for(ada, allowed=(mine,))
    for read_or_write in ("t_read", "t_write"):
        answers: list[str] = []
        for asked in (secret, SECRET_LABEL, SECRET_LABEL.upper(), "0" * 32, "no such label"):
            result = await call(client, token, read_or_write, connection=asked)
            assert result["isError"] is True
            answers.append(text_of(result))
        assert len(set(answers)) == 1, answers  # the same answer whether it exists or not
        assert SECRET_LABEL not in answers[0] and secret not in answers[0]
    assert secret not in loads.ids
    ok = await call(client, token, "t_read", connection="Beach houses")
    assert ok["structuredContent"] == {"connection_id": mine}


async def test_every_answer_leaves_out_connections_the_token_does_not_allow(
    client: httpx.AsyncClient,
) -> None:
    ada = await person("ada@example.test")
    mine = await connection("Beach houses")
    secret = await connection(SECRET_LABEL)
    token, _ = await token_for(ada, allowed=(mine,))
    seen = [
        (await rpc(client, token, "tools/list")).text,
        json.dumps(await call(client, token, "list_connections")),
        json.dumps(await call(client, token, "t_read", connection="x")),
        json.dumps(await call(client, token, "t_read")),
    ]
    for text in seen:
        assert SECRET_LABEL not in text and secret not in text


async def test_two_assistants_of_different_people_see_only_their_own_connections(
    client: httpx.AsyncClient,
) -> None:
    ada = await person("ada@example.test")
    bob = await person("bob@example.test")
    one = await connection("One")
    two = await connection("Two")
    ada_token, _ = await token_for(ada, allowed=(one,))
    bob_token, _ = await token_for(bob, allowed=(two,))
    for token, expected in ((ada_token, "One"), (bob_token, "Two")):
        shown = (await call(client, token, "list_connections"))["structuredContent"]
        assert [item["label"] for item in shown["connections"]] == [expected]
    # Bob's token cannot reach Ada's connection by id or label, and the log says who asked.
    refused = await call(client, bob_token, "t_read", connection=one)
    assert "no connection with that name or id" in text_of(refused)
    last = (await audit_rows())[-1]
    assert last.user_id == bob and last.status == "denied" and last.connection_id is None


async def test_an_approval_with_no_connections_reaches_nothing(
    client: httpx.AsyncClient, loads: Loads
) -> None:
    ada = await person("ada@example.test")
    one = await connection("One")
    token, _ = await token_for(ada)
    assert (await call(client, token, "list_connections"))["structuredContent"] == {
        "connections": []
    }
    for asked in (None, one, "One"):
        arguments = {} if asked is None else {"connection": asked}
        result = await call(client, token, "t_read", **arguments)
        assert result["isError"] is True
        assert "No connections are available" in text_of(result) or (
            "no connection with that name or id" in text_of(result)
        )
    assert loads.ids == []


async def test_all_connections_covers_later_ones_but_a_chosen_list_does_not(
    client: httpx.AsyncClient,
) -> None:
    ada = await person("ada@example.test")
    first = await connection("First")
    everything, _ = await token_for(ada, everything=True)
    chosen, _ = await token_for(ada, allowed=(first,))
    later = await connection("Later")
    shown_all = (await call(client, everything, "list_connections"))["structuredContent"]
    shown_chosen = (await call(client, chosen, "list_connections"))["structuredContent"]
    assert [item["id"] for item in shown_all["connections"]] == [first, later]
    assert [item["id"] for item in shown_chosen["connections"]] == [first]
    assert (await call(client, chosen, "t_read", connection="Later"))["isError"] is True


async def test_changes_stay_off_until_an_administrator_allows_them_per_connection(
    client: httpx.AsyncClient, loads: Loads
) -> None:
    ada = await person("ada@example.test")
    locked = await connection("Locked")
    open_one = await connection("Open", writes=True)
    token, _ = await token_for(ada, allowed=(locked, open_one))
    refused = await call(client, token, "t_write", connection="Locked")
    assert refused["isError"] is True and "Changes are not allowed" in text_of(refused)
    assert locked not in loads.ids
    assert (await call(client, token, "t_write", connection="Open"))["isError"] is False
    assert (await call(client, token, "t_read", connection="Locked"))["isError"] is False


async def test_a_connection_that_needs_attention_is_not_used(
    client: httpx.AsyncClient, loads: Loads
) -> None:
    ada = await person("ada@example.test")
    broken = await connection("Broken", status="needs_attention", writes=True)
    off = await connection("Off", status="disabled")
    token, _ = await token_for(ada, allowed=(broken, off))
    for label in ("Broken", "Off"):
        result = await call(client, token, "t_read", connection=label)
        assert result["isError"] is True and "administrator must fix it" in text_of(result)
    assert loads.ids == []  # type: ignore[attr-defined]


async def test_revoking_the_grant_ends_access_at_once(client: httpx.AsyncClient) -> None:
    ada = await person("ada@example.test")
    one = await connection("One")
    token, grant = await token_for(ada, allowed=(one,))
    assert (await call(client, token, "t_read"))["isError"] is False
    async with write_transaction() as db:
        await db.execute(
            update(OAuthGrant).where(OAuthGrant.id == grant).values(revoked_at=datetime.now(UTC))
        )
    assert (
        await rpc(client, token, "tools/call", {"name": "t_read", "arguments": {}})
    ).status_code == 401


async def test_disabling_the_person_ends_access_at_once(client: httpx.AsyncClient) -> None:
    ada = await person("ada@example.test")
    one = await connection("One")
    token, _ = await token_for(ada, allowed=(one,))
    assert (await call(client, token, "t_read"))["isError"] is False
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == ada).values(is_active=False))
    assert (
        await rpc(client, token, "tools/call", {"name": "t_read", "arguments": {}})
    ).status_code == 401


async def test_a_token_meant_for_another_server_is_refused(client: httpx.AsyncClient) -> None:
    ada = await person("ada@example.test")
    token, _ = await token_for(ada)
    async with write_transaction() as db:
        await db.execute(update(OAuthToken).values(resource="https://elsewhere.example.test/mcp"))
    assert (await rpc(client, token, "tools/list")).status_code == 401


async def test_arguments_cannot_name_another_person_or_widen_the_grant(
    client: httpx.AsyncClient,
) -> None:
    ada = await person("ada@example.test")
    bob = await person("bob@example.test")
    one = await connection("One")
    secret = await connection(SECRET_LABEL)
    token, _ = await token_for(ada, allowed=(one,))
    result = await call(
        client,
        token,
        "t_read",
        connection=SECRET_LABEL,
        user_id=bob,
        all_connections=True,
        allowed_connection_ids=[secret],
    )
    assert result["isError"] is True and secret not in text_of(result)
    assert all(row.user_id == ada for row in await audit_rows())
