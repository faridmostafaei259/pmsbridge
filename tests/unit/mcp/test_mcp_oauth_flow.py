"""End to end: sign in, register an assistant, approve it, get a token, then use /mcp with the
MCP SDK's own client over HTTP."""

import asyncio
import base64
import hashlib
import html
import re
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_helpers import BASE_URL, PASSWORD, seed_connection, seed_user

from pmsbridge.app import create_app
from pmsbridge.core.storage import reset_database

CALLBACK = "https://assistant.example.test/callback"
VERIFIER = "fake-verifier-0123456789-0123456789-0123456789-abcdef"
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:  # replaces the default fixture: more features
    application = create_app(
        (
            "pmsbridge.core.web",
            "pmsbridge.accounts.web",
            "pmsbridge.oauth.web",
            "pmsbridge.pms.connections",
            "pmsbridge.mcp.server",
        )
    )
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


def csrf(text: str) -> str:
    match = CSRF_FIELD.search(text)
    assert match
    return match.group(1)


def challenge() -> str:
    digest = hashlib.sha256(VERIFIER.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def get_access_token(app: FastAPI, email: str, connection_id: str) -> str:
    browser = httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.5", 1)), base_url=BASE_URL
    )
    api = httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.7", 1)), base_url=BASE_URL
    )
    async with browser, api:
        page = await browser.get("/login")
        signed_in = await browser.post(
            "/login",
            data={"email": email, "password": PASSWORD, "csrf_token": csrf(page.text)},
            headers={"Origin": BASE_URL},
        )
        assert signed_in.status_code == 303
        registered = await api.post(
            "/oauth/register",
            json={
                "client_name": "Example Assistant",
                "redirect_uris": [CALLBACK],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge(),
            "code_challenge_method": "S256",
            "state": "s",
            "resource": f"{BASE_URL}/mcp",
        }
        consent = await browser.get("/oauth/authorize", params=params)
        assert consent.status_code == 200, consent.text
        assert "Beach houses" in consent.text  # the person chooses among connections
        hidden = {
            name: html.unescape(value)
            for name, value in re.findall(
                r'<input type="hidden" name="(?!csrf_token)([^"]+)" value="([^"]*)"', consent.text
            )
        }
        decided = await browser.post(
            "/oauth/authorize",
            data={
                **hidden,
                "csrf_token": csrf(consent.text),
                "decision": "approve",
                "mode": "selected",
                "connection": connection_id,
            },
            headers={"Origin": BASE_URL},
        )
        assert decided.status_code == 303, decided.text
        code = parse_qs(urlsplit(decided.headers["location"]).query)["code"][0]
        issued = await api.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "code_verifier": VERIFIER,
                "client_id": client_id,
                "resource": f"{BASE_URL}/mcp",
            },
        )
        assert issued.status_code == 200, issued.text
        return str(issued.json()["access_token"])


async def test_an_assistant_that_completed_sign_in_can_list_and_call_tools(app: FastAPI) -> None:
    await seed_user("ada@example.test")
    mine = await seed_connection("Beach houses", writes_enabled=True)
    await seed_connection("Not chosen")
    token = await get_access_token(app, "ada@example.test", mine)

    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
    )
    connected = Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http))
    async with asyncio.timeout(30), http, connected as client:
        listed = await client.list_tools()
        names = [tool.name for tool in listed.tools]
        assert "list_connections" in names
        tool = next(item for item in listed.tools if item.name == "list_connections")
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        result = await client.call_tool("list_connections", {})
        assert result.is_error is False
        assert result.structured_content is not None
        shown = result.structured_content["connections"]
        assert [item["label"] for item in shown] == ["Beach houses"]  # only the chosen one


async def test_the_unauthenticated_challenge_leads_to_the_metadata_that_names_the_server(
    app: FastAPI,
) -> None:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        refused = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        assert refused.status_code == 401
        match = re.search(r'resource_metadata="([^"]+)"', refused.headers["www-authenticate"])
        assert match
        path = urlsplit(match.group(1)).path
        document = (await client.get(path)).json()
        assert document["resource"] == f"{BASE_URL}/mcp"
        assert document["authorization_servers"] == [BASE_URL]
