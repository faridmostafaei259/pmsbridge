"""The revocation endpoint (RFC 7009) and the metadata that advertises it."""

import base64
from urllib.parse import quote_plus

import httpx
import pytest
from oauth_helpers import (
    BASE_URL,
    code_form,
    connect,
    get_code,
    register_client,
    register_secret_client,
    token_request,
)
from sqlalchemy import select

from pmsbridge.accounts.sessions import hash_token
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth.db import OAuthClient, OAuthGrant
from pmsbridge.oauth.tokens import validate_access_token


@pytest.fixture
async def api(make_browser: object) -> httpx.AsyncClient:
    return make_browser("198.51.100.7")  # type: ignore[operator, no-any-return]


async def revoke(
    api: httpx.AsyncClient, client_id: str, token: str, **extra: str
) -> httpx.Response:
    return await api.post("/oauth/revoke", data={"token": token, "client_id": client_id, **extra})


async def grant_revoked() -> bool:
    async with session() as db:
        return (await db.execute(select(OAuthGrant))).scalar_one().revoked_at is not None


async def test_revoking_an_access_token_ends_only_that_token(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    response = await revoke(api, client_id, body["access_token"], token_type_hint="access_token")  # noqa: S106
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert await validate_access_token(body["access_token"]) is None
    assert not await grant_revoked()
    refreshed = await token_request(
        api,
        {
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200


async def test_revoking_a_refresh_token_revokes_the_grant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    assert (await revoke(api, client_id, body["refresh_token"])).status_code == 200
    assert await grant_revoked()
    assert await validate_access_token(body["access_token"]) is None


async def test_unknown_and_foreign_tokens_get_the_same_answer_and_change_nothing(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    other = str((await register_client(api))["client_id"])
    for token in ("n" * 43, "short", "x" * 1000, body["refresh_token"]):
        answer = await revoke(api, other if token == body["refresh_token"] else client_id, token)
        assert answer.status_code == 200
        assert answer.content == b""
    assert not await grant_revoked()
    assert await validate_access_token(body["access_token"]) is not None


async def test_the_revocation_endpoint_authenticates_the_client(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    _client_id, body = await connect(browser, api)
    unknown = await revoke(api, "u" * 24, body["access_token"])
    assert (unknown.status_code, unknown.json()["error"]) == (400, "invalid_client")
    assert await validate_access_token(body["access_token"]) is not None
    secret_id, secret = await register_secret_client(api)
    refused = await revoke(api, secret_id, "t" * 43, client_secret="wrong" * 8)
    assert refused.json()["error"] == "invalid_client"
    accepted = await revoke(api, secret_id, "t" * 43, client_secret=secret)
    assert accepted.status_code == 200


async def test_a_revocation_without_a_token_is_an_error(api: httpx.AsyncClient) -> None:
    client_id = str((await register_client(api))["client_id"])
    response = await api.post("/oauth/revoke", data={"client_id": client_id})
    assert (response.status_code, response.json()["error"]) == (400, "invalid_request")


async def test_the_metadata_advertises_revocation(api: httpx.AsyncClient) -> None:
    document = (await api.get("/.well-known/oauth-authorization-server")).json()
    assert document["revocation_endpoint"] == f"{BASE_URL}/oauth/revoke"
    assert document["token_endpoint"] == f"{BASE_URL}/oauth/token"
    assert "none" in document["revocation_endpoint_auth_methods_supported"]


def basic(client_id: str, secret: str) -> dict[str, str]:
    raw = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def test_revocation_over_basic_for_a_basic_client(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, secret = await register_secret_client(api, "client_secret_basic")
    code = await get_code(browser, client_id)
    body = (
        await token_request(
            api, code_form(client_id, code, anonymous=True), basic(client_id, secret)
        )
    ).json()
    form = {"token": body["refresh_token"]}
    wrong = await api.post("/oauth/revoke", data=form, headers=basic(client_id, "nope" * 8))
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"].startswith("Basic")
    assert await validate_access_token(body["access_token"]) is not None
    in_body = await api.post(
        "/oauth/revoke", data={**form, "client_id": client_id, "client_secret": secret}
    )
    assert in_body.json()["error"] == "invalid_client"
    assert not await grant_revoked()
    done = await api.post("/oauth/revoke", data=form, headers=basic(client_id, secret))
    assert (done.status_code, done.content) == (200, b"")
    assert await grant_revoked()
    assert await validate_access_token(body["access_token"]) is None


async def test_revocation_over_basic_decodes_percent_encoded_credentials(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, _ = await register_secret_client(api, "client_secret_basic")
    secret = "r3v:oke+ s@cret/ok"  # noqa: S105
    async with write_transaction() as db:
        client = await db.get(OAuthClient, client_id)
        assert client is not None
        client.client_secret_hash = hash_token(secret)
    code = await get_code(browser, client_id)
    issued = (
        await token_request(
            api, code_form(client_id, code, anonymous=True), basic(client_id, secret)
        )
    ).json()
    done = await api.post(
        "/oauth/revoke", data={"token": issued["access_token"]}, headers=basic(client_id, secret)
    )
    assert done.status_code == 200
    assert await validate_access_token(issued["access_token"]) is None
    assert not await grant_revoked()
