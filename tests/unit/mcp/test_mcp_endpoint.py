"""The /mcp endpoint: what a missing, wrong, expired or revoked token gets, and what works."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from mcp_helpers import (
    BASE_URL,
    MCP_HEADERS,
    bearer,
    call_tool,
    disable_user,
    mint_token,
    result_text,
    rpc,
    seed_connection,
    seed_user,
)
from sqlalchemy import select, update

from pmsbridge.core.audit import AuditLog
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth.db import OAuthGrant, OAuthToken

METADATA = f"{BASE_URL}/.well-known/oauth-protected-resource/mcp"


def challenge(response: httpx.Response) -> str:
    assert response.status_code == 401
    return response.headers["www-authenticate"]


async def test_no_token_gets_401_pointing_to_the_resource_metadata(
    client: httpx.AsyncClient,
) -> None:
    response = await rpc(client, None, "tools/list")
    assert challenge(response) == f'Bearer resource_metadata="{METADATA}"'
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("value", ["", "Bearer", "Bearer ", "Basic dXNlcjpwdw==", "Token abc"])
async def test_other_authorization_values_are_the_same_as_no_token(
    client: httpx.AsyncClient, value: str
) -> None:
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**MCP_HEADERS, "Authorization": value},
    )
    if value.lower().startswith("bearer"):
        assert challenge(response).startswith(f'Bearer resource_metadata="{METADATA}"')
    else:
        assert challenge(response) == f'Bearer resource_metadata="{METADATA}"'


@pytest.mark.parametrize("token", ["junk", "x" * 43, "x" * 5000, "a b c"])
async def test_a_wrong_token_gets_401_with_invalid_token(
    client: httpx.AsyncClient, token: str
) -> None:
    response = await rpc(client, token, "tools/list")
    assert challenge(response) == f'Bearer resource_metadata="{METADATA}", error="invalid_token"'
    assert token not in response.text


async def test_a_good_token_reaches_the_tools(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    response = await rpc(client, token, "tools/list")
    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert "list_connections" in names


async def test_the_initialize_handshake_works(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    response = await rpc(
        client,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "pmsbridge"
    assert "mcp-session-id" not in response.headers  # stateless: nothing to remember


async def test_an_expired_token_is_refused(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user, lifetime=timedelta(seconds=-5))
    assert "invalid_token" in challenge(await rpc(client, token, "tools/list"))


async def test_a_token_stops_working_when_it_is_revoked(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, grant_id = await mint_token(user)
    assert (await rpc(client, token, "tools/list")).status_code == 200
    async with write_transaction() as db:
        await db.execute(
            update(OAuthToken).where(OAuthToken.grant_id == grant_id).values(revoked_at=_now())
        )
    assert "invalid_token" in challenge(await rpc(client, token, "tools/list"))


async def test_a_token_stops_working_when_its_grant_is_revoked(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, grant_id = await mint_token(user)
    assert (await rpc(client, token, "tools/list")).status_code == 200
    async with write_transaction() as db:
        await db.execute(
            update(OAuthGrant).where(OAuthGrant.id == grant_id).values(revoked_at=_now())
        )
    assert "invalid_token" in challenge(await rpc(client, token, "tools/list"))


async def test_a_token_stops_working_when_the_person_is_disabled(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    assert (await rpc(client, token, "tools/list")).status_code == 200
    await disable_user(user)
    assert "invalid_token" in challenge(await rpc(client, token, "tools/list"))


async def test_a_token_for_another_resource_is_refused(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    async with write_transaction() as db:
        await db.execute(update(OAuthToken).values(resource="https://other.example.test/mcp"))
    assert "invalid_token" in challenge(await rpc(client, token, "tools/list"))


async def test_a_refresh_token_cannot_call_tools(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    async with write_transaction() as db:
        await db.execute(update(OAuthToken).values(kind="refresh"))
    assert "invalid_token" in challenge(await rpc(client, token, "tools/list"))


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH"])
async def test_only_post_is_accepted(client: httpx.AsyncClient, method: str) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    response = await client.request(method, "/mcp", headers=bearer(token))
    assert response.status_code == 405 and response.headers["allow"] == "POST"


async def test_head_is_not_accepted(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    for headers in (bearer(token), {}):
        response = await client.head("/mcp", headers=headers)
        assert response.status_code == 405 and response.headers["allow"] == "POST"


async def test_a_trailing_slash_never_reaches_a_tool(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_connections"},
    }
    for headers in ({**MCP_HEADERS}, bearer(token)):
        response = await client.post("/mcp/", json=body, headers=headers)
        assert response.status_code != 200
        assert "result" not in response.text
    assert await audit_count() == 0


async def audit_count() -> int:
    async with session() as db:
        return len((await db.scalars(select(AuditLog.id))).all())


async def post_with_origin(
    client: httpx.AsyncClient, token: str | None, origin: str | None
) -> httpx.Response:
    headers = bearer(token) if token is not None else dict(MCP_HEADERS)
    if origin is not None:
        headers["Origin"] = origin
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_connections"},
    }
    return await client.post("/mcp", json=body, headers=headers)


@pytest.mark.parametrize(
    "origin",
    [
        "https://some-web-page.example.test",
        "http://pms.example.test",
        "https://pms.example.test:8443",
        "https://pms.example.test.evil.example",
        "https://pms.example.test/path",
        "null",
        "not an origin",
        "",
    ],
)
async def test_a_foreign_origin_is_refused_before_anything_else(
    client: httpx.AsyncClient, origin: str
) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    for sent in (token, None):
        response = await post_with_origin(client, sent, origin)
        assert response.status_code == 403
        assert "www-authenticate" not in response.headers
        assert "result" not in response.text
    assert await audit_count() == 0


@pytest.mark.parametrize(
    "origin",
    [None, "https://pms.example.test", "HTTPS://PMS.EXAMPLE.TEST", "https://pms.example.test:443"],
)
async def test_no_origin_or_the_servers_own_origin_is_accepted(
    client: httpx.AsyncClient, origin: str | None
) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    response = await post_with_origin(client, token, origin)
    assert response.status_code == 200


async def test_no_cors_headers_are_sent(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    headers = {**bearer(token), "Origin": "https://some-web-page.example.test"}
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
    )
    assert not any(name.startswith("access-control") for name in response.headers)
    preflight = await client.options("/mcp", headers={"Origin": "https://x.example.test"})
    assert "access-control-allow-origin" not in preflight.headers


async def test_a_request_without_json_content_is_refused(client: httpx.AsyncClient) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    response = await client.post(
        "/mcp",
        content="hello",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
    )
    assert response.status_code == 400


async def test_an_unknown_tool_is_an_error_result_without_details(
    client: httpx.AsyncClient,
) -> None:
    user = await seed_user()
    token, _ = await mint_token(user)
    result = await call_tool(client, token, "no_such_tool")
    assert result["isError"] is True and "Traceback" not in result_text(result)


async def test_health_endpoints_do_not_need_a_token(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    assert (await client.get("/healthz")).status_code == 200


async def test_each_request_is_checked_on_its_own(client: httpx.AsyncClient) -> None:
    """One person's token is never reused for the next request that has none."""
    user = await seed_user()
    cid = await seed_connection()
    token, _ = await mint_token(user, connections=(cid,))
    assert (await rpc(client, token, "tools/list")).status_code == 200
    assert (await rpc(client, None, "tools/list")).status_code == 401
    assert (await call_tool(client, token, "list_connections"))["isError"] is False


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
async def test_a_foreign_origin_is_refused_before_the_method_is_checked(
    client: httpx.AsyncClient, method: str
) -> None:
    response = await client.request(method, "/mcp", headers={"Origin": "https://evil.example.test"})
    assert response.status_code == 403
    assert "allow" not in response.headers
