"""Refresh tokens: rotation, reuse detection, expiry, the 90 day cap and revocation."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from oauth_helpers import (
    BOTH_GRANTS,
    code_form,
    connect,
    get_code,
    register_client,
    register_secret_client,
    token_request,
)
from sqlalchemy import select, update

from pmsbridge.accounts import twofactor
from pmsbridge.accounts.db import User
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth.config import mcp_resource
from pmsbridge.oauth.db import OAuthClient, OAuthGrant, OAuthToken
from pmsbridge.oauth.tokens import validate_access_token


@pytest.fixture
async def api(make_browser: object) -> httpx.AsyncClient:
    return make_browser("198.51.100.7")  # type: ignore[operator, no-any-return]


def refresh_form(client_id: str, token: str, **extra: str) -> dict[str, str]:
    return {"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id, **extra}


async def grant_revoked() -> bool:
    async with session() as db:
        return (await db.execute(select(OAuthGrant))).scalar_one().revoked_at is not None


async def test_a_refresh_returns_new_tokens_and_retires_the_old_one(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, first = await connect(browser, api)
    response = await token_request(api, refresh_form(client_id, first["refresh_token"]))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    assert await validate_access_token(second["access_token"]) is not None
    async with session() as db:
        rows = list(
            (await db.execute(select(OAuthToken).where(OAuthToken.kind == "refresh"))).scalars()
        )
    old = [row for row in rows if row.replaced_by is not None]
    assert len(old) == 1 and old[0].revoked_at is not None
    assert not await grant_revoked()


async def test_the_chain_keeps_working(browser: httpx.AsyncClient, api: httpx.AsyncClient) -> None:
    client_id, body = await connect(browser, api)
    for _ in range(3):
        response = await token_request(api, refresh_form(client_id, body["refresh_token"]))
        assert response.status_code == 200
        body = response.json()


async def test_reusing_an_old_refresh_token_revokes_the_grant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, first = await connect(browser, api)
    second = (await token_request(api, refresh_form(client_id, first["refresh_token"]))).json()
    reuse = await token_request(api, refresh_form(client_id, first["refresh_token"]))
    assert (reuse.status_code, reuse.json()["error"]) == (400, "invalid_grant")
    assert await grant_revoked()
    # The newest tokens died with the grant.
    assert await validate_access_token(second["access_token"]) is None
    after = await token_request(api, refresh_form(client_id, second["refresh_token"]))
    assert after.json()["error"] == "invalid_grant"


async def test_two_simultaneous_refreshes_let_exactly_one_through(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    answers = await asyncio.gather(
        *(token_request(api, refresh_form(client_id, body["refresh_token"])) for _ in range(2))
    )
    assert sorted(answer.status_code for answer in answers) == [200, 400]
    assert await grant_revoked()


async def test_another_client_cannot_use_the_refresh_token(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    other = str((await register_client(api, grant_types=BOTH_GRANTS))["client_id"])
    response = await token_request(api, refresh_form(other, body["refresh_token"]))
    assert response.json()["error"] == "invalid_grant"
    assert not await grant_revoked()
    assert (
        await token_request(api, refresh_form(client_id, body["refresh_token"]))
    ).status_code == 200


async def test_an_access_token_is_not_a_refresh_token(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    response = await token_request(api, refresh_form(client_id, body["access_token"]))
    assert response.json()["error"] == "invalid_grant"
    assert not await grant_revoked()


async def test_an_expired_refresh_token_is_refused(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    async with write_transaction() as db:
        await db.execute(
            update(OAuthToken)
            .where(OAuthToken.kind == "refresh")
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    response = await token_request(api, refresh_form(client_id, body["refresh_token"]))
    assert response.json()["error"] == "invalid_grant"


async def test_refresh_tokens_last_30_days_and_never_pass_90_days_from_the_grant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    async with session() as db:
        refresh = (
            await db.execute(select(OAuthToken).where(OAuthToken.kind == "refresh"))
        ).scalar_one()
    assert timedelta(days=29) < refresh.expires_at - refresh.created_at <= timedelta(days=30)
    # A grant made 80 days ago: the next refresh token ends at day 90, not day 110.
    made = datetime.now(UTC) - timedelta(days=80)
    async with write_transaction() as db:
        await db.execute(update(OAuthGrant).values(created_at=made))
    response = await token_request(api, refresh_form(client_id, body["refresh_token"]))
    assert response.status_code == 200
    async with session() as db:
        newest = (
            await db.execute(
                select(OAuthToken).where(
                    OAuthToken.kind == "refresh", OAuthToken.replaced_by.is_(None)
                )
            )
        ).scalar_one()
    assert abs((newest.expires_at - (made + timedelta(days=90))).total_seconds()) < 1
    # Past day 90 nothing can be refreshed, even with an unexpired token.
    async with write_transaction() as db:
        await db.execute(
            update(OAuthGrant).values(created_at=datetime.now(UTC) - timedelta(days=91))
        )
        await db.execute(
            update(OAuthToken).values(expires_at=datetime.now(UTC) + timedelta(days=1))
        )
    late = await token_request(api, refresh_form(client_id, response.json()["refresh_token"]))
    assert late.json()["error"] == "invalid_grant"


async def test_access_tokens_expire_after_an_hour(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    _client_id, body = await connect(browser, api)
    async with session() as db:
        access = (
            await db.execute(select(OAuthToken).where(OAuthToken.kind == "access"))
        ).scalar_one()
    assert access.expires_at - access.created_at == timedelta(hours=1)
    assert access.resource == mcp_resource()
    principal = await validate_access_token(body["access_token"])
    assert principal is not None
    async with write_transaction() as db:
        await db.execute(
            update(OAuthToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await validate_access_token(body["access_token"]) is None


async def test_a_refresh_updates_the_clients_last_use(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    old = datetime.now(UTC) - timedelta(days=30)
    async with write_transaction() as db:
        await db.execute(update(OAuthClient).values(last_used_at=old))
    await token_request(api, refresh_form(client_id, body["refresh_token"]))
    async with session() as db:
        client = await db.get(OAuthClient, client_id)
    assert client is not None and client.last_used_at is not None
    assert client.last_used_at > old + timedelta(days=29)


async def test_refresh_needs_the_client_secret_of_a_confidential_client(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, secret = await register_secret_client(api, "client_secret_post")
    code = await get_code(browser, client_id)
    first = (await token_request(api, code_form(client_id, code, client_secret=secret))).json()
    bare = await token_request(api, refresh_form(client_id, first["refresh_token"]))
    assert bare.json()["error"] == "invalid_client"
    ok = await token_request(
        api, refresh_form(client_id, first["refresh_token"], client_secret=secret)
    )
    assert ok.status_code == 200


async def test_refresh_works_with_client_secret_basic(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, secret = await register_secret_client(api, "client_secret_basic")
    header = {
        "Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    }
    code = await get_code(browser, client_id)
    first = (await token_request(api, code_form(client_id, code, anonymous=True), header)).json()
    form = {"grant_type": "refresh_token", "refresh_token": first["refresh_token"]}
    in_body = await token_request(api, {**form, "client_id": client_id, "client_secret": secret})
    assert in_body.json()["error"] == "invalid_client"
    wrong = {"Authorization": "Basic " + base64.b64encode(f"{client_id}:nope".encode()).decode()}
    refused = await token_request(api, form, wrong)
    assert (refused.status_code, refused.json()["error"]) == (401, "invalid_client")
    ok = await token_request(api, form, header)
    assert ok.status_code == 200
    assert ok.json()["refresh_token"] != first["refresh_token"]
    assert await validate_access_token(ok.json()["access_token"]) is not None


async def make_admin(person: int) -> None:
    async with write_transaction() as db:
        user = await db.get(User, person)
        assert user is not None
        user.role = "admin"


async def test_an_administrator_without_two_factor_cannot_refresh_and_loses_the_grant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, person: int
) -> None:
    client_id, body = await connect(browser, api)
    await make_admin(person)
    response = await token_request(api, refresh_form(client_id, body["refresh_token"]))
    assert (response.status_code, response.json()["error"]) == (400, "invalid_grant")
    assert await grant_revoked()
    assert await validate_access_token(body["access_token"]) is None


async def test_an_administrator_with_two_factor_can_refresh(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, person: int
) -> None:
    client_id, body = await connect(browser, api)
    async with write_transaction() as db:
        twofactor.add_totp(db, person, b"fake-totp-secret-0123", 0)
        user = await db.get(User, person)
        assert user is not None
        user.role = "admin"
    response = await token_request(api, refresh_form(client_id, body["refresh_token"]))
    assert response.status_code == 200
    assert not await grant_revoked()


async def test_a_grant_may_be_refreshed_ten_times_a_minute(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    for _ in range(10):
        response = await token_request(api, refresh_form(client_id, body["refresh_token"]))
        assert response.status_code == 200
        body = response.json()
    limited = await token_request(api, refresh_form(client_id, body["refresh_token"]))
    assert (limited.status_code, limited.json()["error"]) == (429, "temporarily_unavailable")
    assert 1 <= int(limited.headers["retry-after"]) <= 61
    assert limited.headers["cache-control"] == "no-store"
    assert "refresh_token" not in limited.json()
    # Being told to wait does not use the token up or revoke the approval.
    assert not await grant_revoked()
    assert await validate_access_token(body["access_token"]) is not None
    async with write_transaction() as db:
        await db.execute(
            update(OAuthToken)
            .where(OAuthToken.revoked_at.is_not(None))
            .values(revoked_at=datetime.now(UTC) - timedelta(minutes=2))
        )
    later = await token_request(api, refresh_form(client_id, body["refresh_token"]))
    assert later.status_code == 200


async def test_the_refresh_limit_is_per_grant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    other_id, other = await connect(browser, api)
    for _ in range(10):
        body = (await token_request(api, refresh_form(client_id, body["refresh_token"]))).json()
    assert (
        await token_request(api, refresh_form(client_id, body["refresh_token"]))
    ).status_code == 429
    assert (
        await token_request(api, refresh_form(other_id, other["refresh_token"]))
    ).status_code == 200
