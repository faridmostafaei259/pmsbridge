"""Authorization codes: single use, binding to the client, address and PKCE verifier."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from oauth_helpers import (
    CALLBACK,
    VERIFIER,
    authorize_params,
    location_query,
    register_client,
)
from oauth_helpers import approve as approve_request
from sqlalchemy import select, update

from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth.db import OAuthCode, OAuthGrant
from pmsbridge.oauth.grants import redeem_code


async def issue(browser: httpx.AsyncClient) -> tuple[str, str]:
    """Approve a request and return (client_id, code)."""
    client_id = str((await register_client(browser))["client_id"])
    response = await approve_request(browser, authorize_params(client_id))
    return client_id, location_query(response)["code"]


async def grant_revoked() -> bool:
    async with session() as db:
        grant = (await db.execute(select(OAuthGrant))).scalar_one()
    return grant.revoked_at is not None


async def code_used() -> bool:
    async with session() as db:
        row = (await db.execute(select(OAuthCode))).scalar_one()
    return row.used_at is not None


async def test_a_code_works_once(browser: httpx.AsyncClient, person: int) -> None:
    client_id, code = await issue(browser)
    first = await redeem_code(code, client_id, CALLBACK, VERIFIER)
    assert first is not None
    assert (first.user_id, first.client_id, first.redirect_uri) == (person, client_id, CALLBACK)
    assert await redeem_code(code, client_id, CALLBACK, VERIFIER) is None


async def test_using_a_code_twice_revokes_the_grant(browser: httpx.AsyncClient) -> None:
    client_id, code = await issue(browser)
    await redeem_code(code, client_id, CALLBACK, VERIFIER)
    assert not await grant_revoked()
    await redeem_code(code, client_id, CALLBACK, VERIFIER)
    assert await grant_revoked()


async def test_replaying_a_used_code_revokes_even_with_wrong_details(
    browser: httpx.AsyncClient,
) -> None:
    client_id, code = await issue(browser)
    await redeem_code(code, client_id, CALLBACK, VERIFIER)
    assert await redeem_code(code, "other-client", "https://evil.example/cb", "x" * 43) is None
    assert await grant_revoked()


async def test_two_simultaneous_redemptions_let_exactly_one_through(
    browser: httpx.AsyncClient,
) -> None:
    client_id, code = await issue(browser)
    results = await asyncio.gather(
        *(redeem_code(code, client_id, CALLBACK, VERIFIER) for _ in range(2))
    )
    assert sum(result is not None for result in results) == 1
    assert await grant_revoked()


async def test_a_wrong_verifier_does_not_use_the_code_up(browser: httpx.AsyncClient) -> None:
    client_id, code = await issue(browser)
    for verifier in ("w" * 43, VERIFIER + "x", "", "short", VERIFIER[:-1]):
        assert await redeem_code(code, client_id, CALLBACK, verifier) is None
    assert not await code_used()
    assert not await grant_revoked()
    assert await redeem_code(code, client_id, CALLBACK, VERIFIER) is not None


async def test_a_wrong_client_or_address_does_not_use_the_code_up(
    browser: httpx.AsyncClient,
) -> None:
    client_id, code = await issue(browser)
    other = str((await register_client(browser))["client_id"])
    for wrong_client, wrong_uri in (
        (other, CALLBACK),
        (client_id, CALLBACK + "/"),
        (client_id, "https://evil.example/callback"),
        (client_id, ""),
    ):
        assert await redeem_code(code, wrong_client, wrong_uri, VERIFIER) is None
    assert not await code_used()
    assert not await grant_revoked()
    assert await redeem_code(code, client_id, CALLBACK, VERIFIER) is not None


async def test_an_expired_code_is_refused(browser: httpx.AsyncClient) -> None:
    client_id, code = await issue(browser)
    async with write_transaction() as db:
        await db.execute(
            update(OAuthCode).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await redeem_code(code, client_id, CALLBACK, VERIFIER) is None


async def test_a_code_of_a_revoked_grant_is_refused(browser: httpx.AsyncClient) -> None:
    client_id, code = await issue(browser)
    async with write_transaction() as db:
        await db.execute(update(OAuthGrant).values(revoked_at=datetime.now(UTC)))
    assert await redeem_code(code, client_id, CALLBACK, VERIFIER) is None


async def test_unknown_and_malformed_codes_are_refused(app: FastAPI) -> None:
    for value in ("", "nope", "x" * 43, "A" * 5000):
        assert await redeem_code(value, "c" * 32, CALLBACK, VERIFIER) is None
