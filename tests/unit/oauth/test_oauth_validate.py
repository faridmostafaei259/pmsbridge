"""validate_access_token: what a good token returns and the reasons a token is refused."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from oauth_helpers import connect
from sqlalchemy import select, update

from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth.db import OAuthGrant
from pmsbridge.oauth.tokens import AccessPrincipal, validate_access_token


@pytest.fixture
async def api(make_browser: object) -> httpx.AsyncClient:
    return make_browser("198.51.100.7")  # type: ignore[operator, no-any-return]


async def test_a_good_token_returns_who_it_speaks_for(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, person: int
) -> None:
    client_id, body = await connect(browser, api)
    principal = await validate_access_token(body["access_token"])
    assert principal is not None
    assert (principal.user_id, principal.client_id, principal.scope) == (person, client_id, "mcp")
    assert principal.all_connections is False
    assert principal.allowed_connection_ids == ()
    remaining = principal.expires_at - datetime.now(UTC)
    assert timedelta(minutes=59) < remaining <= timedelta(hours=1)
    async with session() as db:
        grant = (await db.execute(select(OAuthGrant))).scalar_one()
    assert principal.grant_id == grant.id
    assert principal == AccessPrincipal(
        user_id=person,
        client_id=client_id,
        grant_id=grant.id,
        scope="mcp",
        all_connections=False,
        allowed_connection_ids=(),
        expires_at=principal.expires_at,
    )


async def test_changes_to_the_grants_connections_show_at_once(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    _client_id, body = await connect(browser, api)
    async with write_transaction() as db:
        await db.execute(update(OAuthGrant).values(allowed_connection_ids=["c1", "c2"]))
    principal = await validate_access_token(body["access_token"])
    assert principal is not None and principal.allowed_connection_ids == ("c1", "c2")
    async with write_transaction() as db:
        await db.execute(update(OAuthGrant).values(all_connections=True))
    principal = await validate_access_token(body["access_token"])
    assert principal is not None and principal.all_connections is True


async def test_a_refresh_token_is_not_an_access_token(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    _client_id, body = await connect(browser, api)
    assert await validate_access_token(body["refresh_token"]) is None


@pytest.mark.parametrize("value", ["", "short", "x" * 43, "x" * 5000, "not valid!" * 8, "é" * 43])
async def test_junk_is_refused(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, value: str
) -> None:
    await connect(browser, api)
    assert await validate_access_token(value) is None


async def test_a_token_for_another_resource_is_refused(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client_id, body = await connect(browser, api)
    assert await validate_access_token(body["access_token"]) is not None
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://other.example.test")
    clear_settings_cache()
    assert await validate_access_token(body["access_token"]) is None
