"""Helpers for the MCP tests: seeding people, connections and access tokens, and calling /mcp."""

import json
import secrets
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import update

from pmsbridge.accounts.db import User
from pmsbridge.accounts.service import create_user
from pmsbridge.accounts.sessions import hash_token
from pmsbridge.core.storage import write_transaction
from pmsbridge.oauth.config import mcp_resource
from pmsbridge.oauth.db import OAuthClient, OAuthGrant, OAuthToken
from pmsbridge.pms.db import PMSConnection, sealed_credential_columns

BASE_URL = "https://pms.example.test"
PASSWORD = "fake-passphrase-for-tests"  # noqa: S105
CLIENT_KEY = "test-assistant"
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


async def seed_user(email: str = "ada@example.test", role: str = "staff") -> int:
    return await create_user(email, PASSWORD, role=role)


async def seed_connection(
    label: str = "Beach houses",
    *,
    provider: str = "guesty",
    status: str = "active",
    writes_enabled: bool = False,
) -> str:
    identifier = secrets.token_hex(16)
    now = datetime.now(UTC)
    async with write_transaction() as db:
        db.add(
            PMSConnection(
                id=identifier,
                provider=provider,
                label=label,
                status=status,
                writes_enabled=writes_enabled,
                guesty_account_id=f"acct-{identifier[:8]}",
                created_at=now,
                updated_at=now,
                **sealed_credential_columns(
                    identifier,
                    {"client_id": f"fake-key-{identifier}", "client_secret": "fake-secret"},
                ),
            )
        )
    return identifier


async def mint_token(
    user_id: int,
    *,
    connections: tuple[str, ...] = (),
    all_connections: bool = False,
    lifetime: timedelta = timedelta(hours=1),
    client_key: str = CLIENT_KEY,
    revoke_grant: bool = False,
) -> tuple[str, int]:
    """An access token for a new grant; returns (token, grant id)."""
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    async with write_transaction() as db:
        if await db.get(OAuthClient, client_key) is None:
            db.add(
                OAuthClient(
                    client_id=client_key,
                    client_name="Test assistant",
                    redirect_uris=["https://assistant.example.test/callback"],
                    token_endpoint_auth_method="none",  # noqa: S106
                    grant_types=["authorization_code", "refresh_token"],
                    created_at=now,
                )
            )
        grant = OAuthGrant(
            user_id=user_id,
            client_id=client_key,
            all_connections=all_connections,
            allowed_connection_ids=list(connections),
            scope="mcp",
            resource=mcp_resource(),
            created_at=now,
            revoked_at=now if revoke_grant else None,
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
                expires_at=now + lifetime,
            )
        )
        return token, grant.id


async def disable_user(user_id: int) -> None:
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == user_id).values(is_active=False))


def bearer(token: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


async def rpc(
    client: httpx.AsyncClient,
    token: str | None,
    method: str,
    params: dict[str, object] | None = None,
) -> httpx.Response:
    headers = bearer(token) if token is not None else dict(MCP_HEADERS)
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await client.post("/mcp", content=json.dumps(body), headers=headers)


async def call_tool(
    client: httpx.AsyncClient, token: str, name: str, arguments: dict[str, object] | None = None
) -> dict[str, object]:
    """The `result` of a tools/call (an error result has `isError` true)."""
    response = await rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "result" in body, body
    return body["result"]  # type: ignore[no-any-return]


def result_text(result: dict[str, object]) -> str:
    content = result["content"]
    assert isinstance(content, list) and content
    return " ".join(str(item["text"]) for item in content if item.get("type") == "text")
