"""The token endpoint: code exchange for each client authentication method, errors, limits."""

import base64
import logging
import re
from urllib.parse import quote, quote_plus

import httpx
import pytest
from oauth_helpers import (
    BOTH_GRANTS,
    CALLBACK,
    VERIFIER,
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
from pmsbridge.oauth.db import OAuthClient, OAuthGrant, OAuthToken


@pytest.fixture
async def api(make_browser: object) -> httpx.AsyncClient:
    return make_browser("198.51.100.7")  # type: ignore[operator, no-any-return]


def basic(client_id: str, secret: str) -> dict[str, str]:
    raw = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def test_a_public_client_exchanges_a_code_for_tokens(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id = str((await register_client(api, grant_types=BOTH_GRANTS))["client_id"])
    code = await get_code(browser, client_id)
    response = await token_request(api, code_form(client_id, code))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    body = response.json()
    assert body["token_type"] == "Bearer"  # noqa: S105
    assert body["expires_in"] == 3600
    assert body["scope"] == "mcp"
    assert body["access_token"] != body["refresh_token"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["access_token"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["refresh_token"])


async def test_a_client_secret_post_client_exchanges_a_code(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, secret = await register_secret_client(api, "client_secret_post")
    code = await get_code(browser, client_id)
    wrong = await token_request(api, code_form(client_id, code, client_secret="wrong" * 8))
    assert (wrong.status_code, wrong.json()["error"]) == (400, "invalid_client")
    missing = await token_request(api, code_form(client_id, code))
    assert missing.json()["error"] == "invalid_client"
    # The failed attempts did not use the code up.
    ok = await token_request(api, code_form(client_id, code, client_secret=secret))
    assert ok.status_code == 200


async def test_a_client_secret_basic_client_exchanges_a_code(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, secret = await register_secret_client(api, "client_secret_basic")
    code = await get_code(browser, client_id)
    form = code_form(client_id, code, anonymous=True)
    wrong = await token_request(api, form, basic(client_id, "wrong" * 8))
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"].startswith("Basic")
    both = await token_request(api, {**form, "client_secret": secret}, basic(client_id, secret))
    assert both.json()["error"] == "invalid_request"
    in_body = await token_request(api, {**form, "client_secret": secret, "client_id": client_id})
    assert in_body.json()["error"] == "invalid_client"
    ok = await token_request(api, form, basic(client_id, secret))
    assert ok.status_code == 200


async def test_each_method_must_be_used_as_registered(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    public = await register_client(api)
    code = await get_code(browser, str(public["client_id"]))
    # A public client may not authenticate with a secret it does not have.
    response = await token_request(
        api,
        code_form(str(public["client_id"]), code, client_secret="x" * 20),
    )
    assert response.json()["error"] == "invalid_client"
    posted_id, posted_secret = await register_secret_client(api)
    code = await get_code(browser, posted_id)
    via_header = await token_request(
        api,
        code_form(posted_id, code, anonymous=True),
        basic(posted_id, posted_secret),
    )
    assert via_header.status_code == 401


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"code_verifier": "w" * 43}, "invalid_grant"),
        ({"redirect_uri": "https://assistant.example.test/other"}, "invalid_grant"),
        ({"code_verifier": None}, "invalid_request"),
    ],
)
async def test_wrong_details_leave_the_code_usable(
    browser: httpx.AsyncClient,
    api: httpx.AsyncClient,
    change: dict[str, str | None],
    error: str,
) -> None:
    client_id = str((await register_client(api))["client_id"])
    code = await get_code(browser, client_id)
    response = await token_request(api, code_form(client_id, code, **change))
    assert response.status_code == 400
    assert response.json()["error"] == error
    assert (await token_request(api, code_form(client_id, code))).status_code == 200


async def test_another_client_cannot_use_the_code(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    owner = str((await register_client(api))["client_id"])
    thief = str((await register_client(api))["client_id"])
    code = await get_code(browser, owner)
    stolen = await token_request(api, code_form(thief, code))
    assert (stolen.status_code, stolen.json()["error"]) == (400, "invalid_grant")
    assert (await token_request(api, code_form(owner, code))).status_code == 200


async def test_a_replayed_code_fails_and_revokes_only_its_own_grant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    await connect(browser, api)  # an unrelated live grant
    client_id = str((await register_client(api))["client_id"])
    code = await get_code(browser, client_id)
    assert (await token_request(api, code_form(client_id, code))).status_code == 200
    again = await token_request(api, code_form(client_id, code))
    assert again.json()["error"] == "invalid_grant"
    async with session() as db:
        grants = list((await db.execute(select(OAuthGrant).order_by(OAuthGrant.id))).scalars())
    assert [grant.revoked_at is not None for grant in grants] == [False, True]


async def test_the_resource_must_match_when_sent(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id = str((await register_client(api))["client_id"])
    code = await get_code(browser, client_id)
    wrong = await token_request(
        api, code_form(client_id, code, resource="https://evil.example/mcp")
    )
    assert (wrong.status_code, wrong.json()["error"]) == (400, "invalid_target")
    right = await token_request(
        api, code_form(client_id, code, resource="https://pms.example.test/mcp")
    )
    assert right.status_code == 200


async def test_a_client_without_refresh_registration_gets_no_refresh_token(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id = str((await register_client(api))["client_id"])
    code = await get_code(browser, client_id)
    body = (await token_request(api, code_form(client_id, code))).json()
    assert "refresh_token" not in body
    refresh = await token_request(
        api, {"grant_type": "refresh_token", "refresh_token": "a" * 43, "client_id": client_id}
    )
    assert refresh.json()["error"] == "unauthorized_client"


async def test_malformed_requests_get_oauth_errors(api: httpx.AsyncClient) -> None:
    form = {"grant_type": "authorization_code"}
    cases = [
        (await api.post("/oauth/token", json=form), "invalid_request"),
        (
            await api.post(
                "/oauth/token",
                content=b"x" * 9000,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            "invalid_request",
        ),
        (await token_request(api, {"grant_type": "password"}), "invalid_client"),
        (
            await api.post(
                "/oauth/token",
                content=b"a=1&a=2",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            "invalid_request",
        ),
    ]
    for response, error in cases:
        assert response.json()["error"] == error
        assert response.headers["cache-control"] == "no-store"
    assert (await api.get("/oauth/token")).status_code == 405


async def test_an_unsupported_grant_type_and_scope(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id = str((await register_client(api, grant_types=BOTH_GRANTS))["client_id"])
    response = await token_request(api, {"grant_type": "password", "client_id": client_id})
    assert (response.status_code, response.json()["error"]) == (400, "unsupported_grant_type")
    response = await token_request(
        api,
        {
            "grant_type": "refresh_token",
            "refresh_token": "a" * 43,
            "client_id": client_id,
            "scope": "admin",
        },
    )
    assert response.json()["error"] == "invalid_scope"


async def test_errors_do_not_echo_what_was_sent(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id = str((await register_client(api))["client_id"])
    code = await get_code(browser, client_id)
    response = await token_request(
        api, code_form(client_id, code, code_verifier="v" * 43, redirect_uri=CALLBACK + "/zzzz")
    )
    assert response.status_code == 400
    for secret in (code, "v" * 43, "zzzz"):
        assert secret not in response.text


async def fail_many(api: httpx.AsyncClient, client_id: str, count: int) -> list[httpx.Response]:
    return [await token_request(api, code_form(client_id, "c" * 43)) for _ in range(count)]


async def test_a_public_clients_claimed_id_never_blocks_anyone(
    browser: httpx.AsyncClient, make_browser: object, api: httpx.AsyncClient
) -> None:
    """Anyone can claim a public client's id, so failures under it must not lock it out."""
    client_id = str((await register_client(api))["client_id"])
    answers = await fail_many(api, client_id, 25)
    assert {answer.status_code for answer in answers} == {400}
    assert answers[-1].json()["error"] == "invalid_grant"
    stranger = make_browser("198.51.100.99")  # type: ignore[operator]
    real_code = await get_code(browser, client_id)
    exchanged = await token_request(stranger, code_form(client_id, real_code))
    assert exchanged.status_code == 200


async def test_failures_from_one_address_are_limited_whatever_the_client(
    make_browser: object, api: httpx.AsyncClient
) -> None:
    unknown = "u" * 24
    answers = await fail_many(api, unknown, 31)
    assert {answer.status_code for answer in answers[:30]} == {400}
    limited = answers[30]
    assert (limited.status_code, limited.json()["error"]) == (429, "temporarily_unavailable")
    assert int(limited.headers["retry-after"]) > 0
    assert limited.headers["cache-control"] == "no-store"
    # A different client id from the same address is refused too; another address is not.
    assert (await fail_many(api, "v" * 24, 1))[0].status_code == 429
    elsewhere = make_browser("198.51.100.98")  # type: ignore[operator]
    assert (await fail_many(elsewhere, unknown, 1))[0].status_code == 400


async def test_a_shared_address_gets_a_much_larger_limit(make_browser: object) -> None:
    """Visitors behind the container network all look like one private address."""
    shared = make_browser("10.0.0.5")  # type: ignore[operator]
    answers = await fail_many(shared, "u" * 24, 301)
    assert {answer.status_code for answer in answers[:300]} == {400}
    assert answers[300].status_code == 429


async def test_wrong_secrets_for_a_confidential_client_lock_that_client(
    api: httpx.AsyncClient, make_browser: object
) -> None:
    client_id, secret = await register_secret_client(api, "client_secret_post")
    guesses = [
        await token_request(api, code_form(client_id, "c" * 43, client_secret="g" * 20))
        for _ in range(21)
    ]
    assert {answer.json()["error"] for answer in guesses[:20]} == {"invalid_client"}
    assert guesses[20].status_code == 429
    # The client's own count also stops the right secret from another address for a while.
    elsewhere = make_browser("198.51.100.97")  # type: ignore[operator]
    locked = await token_request(elsewhere, code_form(client_id, "c" * 43, client_secret=secret))
    assert locked.status_code == 429
    # Other clients, public or not, are unaffected.
    other = str((await register_client(elsewhere))["client_id"])
    assert (await token_request(elsewhere, code_form(other, "c" * 43))).status_code == 400


async def test_wrong_codes_do_not_count_against_a_confidential_client(
    api: httpx.AsyncClient, make_browser: object
) -> None:
    """Only a failed secret check counts per client; the address limit still applies."""
    client_id, secret = await register_secret_client(api, "client_secret_post")
    for _ in range(25):
        answer = await token_request(api, code_form(client_id, "c" * 43, client_secret=secret))
        assert (answer.status_code, answer.json()["error"]) == (400, "invalid_grant")
    elsewhere = make_browser("198.51.100.96")  # type: ignore[operator]
    fresh = await token_request(elsewhere, code_form(client_id, "c" * 43, client_secret=secret))
    assert fresh.json()["error"] == "invalid_grant"


async def set_secret(client_id: str, secret: str) -> None:
    async with write_transaction() as db:
        client = await db.get(OAuthClient, client_id)
        assert client is not None
        client.client_secret_hash = hash_token(secret)


async def test_basic_credentials_are_percent_decoded(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    """RFC 6749 section 2.3.1: the id and secret are form-encoded before Base64."""
    client_id, _ = await register_secret_client(api, "client_secret_basic")
    secret = "s3cr:t+ w@rd/\u00e9-ok"  # noqa: S105
    await set_secret(client_id, secret)
    encoded = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()
    good = {"Authorization": "Basic " + base64.b64encode(encoded).decode()}
    raw = {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}
    code = await get_code(browser, client_id)
    form = code_form(client_id, code, anonymous=True)
    assert (await token_request(api, form, raw)).status_code == 401
    assert (await token_request(api, form, good)).status_code == 200


async def test_issuing_updates_the_client_and_stores_only_hashes(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    async with session() as db:
        client = await db.get(OAuthClient, client_id)
        rows = list((await db.execute(select(OAuthToken))).scalars())
    assert client is not None and client.last_used_at is not None
    assert {row.kind for row in rows} == {"access", "refresh"}
    stored = {row.token_hash for row in rows}
    assert body["access_token"] not in stored and body["refresh_token"] not in stored
    assert all(len(row.token_hash) == 64 for row in rows)


async def test_tokens_never_reach_the_logs(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client_id, body = await connect(browser, api)
    await token_request(
        api,
        {
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": client_id,
        },
    )
    await token_request(
        api,
        {
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": client_id,
        },
    )
    text = caplog.text
    assert body["access_token"] not in text and body["refresh_token"] not in text
    assert VERIFIER not in text
