# ruff: noqa: S105  (client secrets in these tests are made-up values)
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from oauth_helpers import CALLBACK, Browser, register_client
from sqlalchemy import select, update

from pmsbridge.accounts.sessions import hash_token
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import clients
from pmsbridge.oauth.db import OAuthClient
from pmsbridge.oauth.registration import (
    GLOBAL_PER_HOUR,
    MAX_BODY_BYTES,
    PER_ADDRESS_PER_HOUR,
    RegistrationLimiter,
)

VALID = {"client_name": "Example Assistant", "redirect_uris": [CALLBACK]}


async def post(client: httpx.AsyncClient, body: object) -> httpx.Response:
    return await client.post("/oauth/register", json=body)


async def test_a_public_client_is_registered_without_a_secret(
    anonymous: httpx.AsyncClient,
) -> None:
    response = await post(anonymous, {**VALID, "token_endpoint_auth_method": "none"})
    assert response.status_code == 201
    body = response.json()
    assert body["client_name"] == "Example Assistant"
    assert body["redirect_uris"] == [CALLBACK]
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code"]
    assert body["response_types"] == ["code"]
    assert "client_secret" not in body
    assert isinstance(body["client_id_issued_at"], int)
    assert response.headers["cache-control"] == "no-store"
    stored = await clients.get_client(body["client_id"])
    assert stored is not None and stored.client_secret_hash is None


async def test_omitting_the_auth_method_registers_a_public_client(
    anonymous: httpx.AsyncClient,
) -> None:
    body = (await post(anonymous, VALID)).json()
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in body


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
async def test_a_secret_is_returned_once_and_stored_as_a_hash(
    anonymous: httpx.AsyncClient, method: str
) -> None:
    body = (
        await post(
            anonymous,
            {
                **VALID,
                "token_endpoint_auth_method": method,
                "grant_types": ["authorization_code", "refresh_token"],
            },
        )
    ).json()
    secret = body["client_secret"]
    assert len(secret) >= 43
    assert body["client_secret_expires_at"] == 0
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    async with session() as db:
        row = (await db.execute(select(OAuthClient))).scalar_one()
    assert row.client_secret_hash == hash_token(secret)
    assert secret not in json.dumps(row.redirect_uris) + row.client_name + row.client_id


async def test_client_ids_are_random_and_unique(anonymous: httpx.AsyncClient) -> None:
    first = (await post(anonymous, VALID)).json()["client_id"]
    second = (await post(anonymous, VALID)).json()["client_id"]
    assert first != second
    assert len(first) >= 32


@pytest.mark.parametrize(
    "uris",
    [
        ["http://assistant.example.test/cb"],
        ["https://assistant.example.test/cb#fragment"],
        ["javascript:alert(1)"],
        ["https://user@assistant.example.test/cb"],
        ["relative/path"],
        [],
        "https://assistant.example.test/cb",
        [5],
        [CALLBACK, "http://evil.example/cb"],
    ],
)
async def test_bad_redirect_uris_are_refused(anonymous: httpx.AsyncClient, uris: object) -> None:
    response = await post(anonymous, {**VALID, "redirect_uris": uris})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


async def test_missing_redirect_uris_are_refused(anonymous: httpx.AsyncClient) -> None:
    response = await post(anonymous, {"client_name": "Example"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


async def test_at_most_ten_redirect_uris(anonymous: httpx.AsyncClient) -> None:
    ten = [f"https://assistant.example.test/cb{n}" for n in range(10)]
    assert (await post(anonymous, {**VALID, "redirect_uris": ten})).status_code == 201
    eleven = [*ten, "https://assistant.example.test/cb10"]
    response = await post(anonymous, {**VALID, "redirect_uris": eleven})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "   ",
        "x" * 101,
        "bad\nname",
        "bad" + chr(0x202E) + "name",
        "bad\x00name",
        5,
        ["a"],
    ],
)
async def test_the_client_name_is_required_and_checked(
    anonymous: httpx.AsyncClient, name: object
) -> None:
    response = await post(anonymous, {**VALID, "client_name": name})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


async def test_the_longest_allowed_name_is_kept_and_trimmed(anonymous: httpx.AsyncClient) -> None:
    body = (await post(anonymous, {**VALID, "client_name": "  " + "x" * 100 + "  "})).json()
    assert body["client_name"] == "x" * 100


@pytest.mark.parametrize(
    "extra",
    [
        {"token_endpoint_auth_method": "private_key_jwt"},
        {"token_endpoint_auth_method": 5},
        {"grant_types": ["implicit"]},
        {"grant_types": ["refresh_token"]},
        {"grant_types": []},
        {"grant_types": ["authorization_code", 5]},
        {"grant_types": ["authorization_code"] * 21},
        {"response_types": ["token"]},
    ],
)
async def test_unsupported_metadata_is_refused(
    anonymous: httpx.AsyncClient, extra: dict[str, object]
) -> None:
    response = await post(anonymous, {**VALID, **extra})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


async def test_grant_and_response_types_this_server_does_not_offer_are_ignored(
    anonymous: httpx.AsyncClient,
) -> None:
    response = await post(
        anonymous,
        {
            **VALID,
            "grant_types": [
                "authorization_code",
                "client_credentials",
                "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "refresh_token",
            ],
            "response_types": ["code", "token"],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["response_types"] == ["code"]


async def test_unused_metadata_fields_are_ignored(anonymous: httpx.AsyncClient) -> None:
    response = await post(
        anonymous,
        {**VALID, "application_type": "native", "logo_uri": "http://x", "client_uri": "x"},
    )
    assert response.status_code == 201
    assert "logo_uri" not in response.json()


async def test_the_body_must_be_json(anonymous: httpx.AsyncClient) -> None:
    form = await anonymous.post("/oauth/register", data={"client_name": "x"})
    assert form.status_code == 415
    broken = await anonymous.post(
        "/oauth/register", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert broken.status_code == 400
    assert broken.json()["error"] == "invalid_client_metadata"
    for body in (b"[]", b'"text"', b"5", b"null"):
        response = await anonymous.post(
            "/oauth/register", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400


async def test_deeply_nested_json_is_refused_cleanly(anonymous: httpx.AsyncClient) -> None:
    body = b"[" * 5000 + b"]" * 5000
    response = await anonymous.post(
        "/oauth/register", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


async def test_an_oversized_body_is_refused(anonymous: httpx.AsyncClient) -> None:
    big = json.dumps({**VALID, "padding": "x" * MAX_BODY_BYTES})
    response = await anonymous.post(
        "/oauth/register", content=big, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


async def test_an_oversized_streamed_body_is_refused(anonymous: httpx.AsyncClient) -> None:
    async def chunks() -> object:
        for _ in range(8):
            yield b" " * (MAX_BODY_BYTES // 4)

    response = await anonymous.post(
        "/oauth/register",
        content=chunks(),  # type: ignore[arg-type]
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


async def test_errors_never_repeat_what_the_caller_sent(anonymous: httpx.AsyncClient) -> None:
    marker = "marker-value-12345"
    response = await post(anonymous, {**VALID, "redirect_uris": [f"ftp://{marker}.example/x"]})
    assert marker not in response.text
    response = await post(anonymous, {**VALID, "client_name": f"{marker}\nx"})
    assert marker not in response.text


async def test_registration_is_limited_per_address(make_browser: Browser) -> None:
    visitor = make_browser("203.0.113.9")
    for _ in range(PER_ADDRESS_PER_HOUR):
        assert (await post(visitor, VALID)).status_code == 201
    refused = await post(visitor, VALID)
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    # Another visitor is not affected.
    assert (await post(make_browser("203.0.113.10"), VALID)).status_code == 201


async def test_invalid_requests_count_towards_the_limit(make_browser: Browser) -> None:
    visitor = make_browser("203.0.113.9")
    for _ in range(PER_ADDRESS_PER_HOUR):
        assert (await post(visitor, {"client_name": ""})).status_code == 400
    assert (await post(visitor, VALID)).status_code == 429


async def test_registration_is_limited_for_the_whole_installation(make_browser: Browser) -> None:
    for number in range(GLOBAL_PER_HOUR):
        visitor = make_browser(f"198.51.100.{number + 1}")
        assert (await post(visitor, VALID)).status_code == 201
    assert (await post(make_browser("198.51.100.200"), VALID)).status_code == 429


async def test_shared_addresses_are_limited_only_by_the_global_limit(
    make_browser: Browser,
) -> None:
    visitor = make_browser("172.18.0.1")  # the container network gateway
    for _ in range(PER_ADDRESS_PER_HOUR + 5):
        assert (await post(visitor, VALID)).status_code == 201


def test_the_limiter_forgets_old_registrations() -> None:
    now = [0.0]
    limiter = RegistrationLimiter(per_address=2, global_limit=100, window=60, clock=lambda: now[0])
    assert limiter.check("203.0.113.1") == 0
    assert limiter.check("203.0.113.1") == 0
    assert limiter.check("203.0.113.1") > 0
    now[0] = 61.0
    assert limiter.check("203.0.113.1") == 0


async def test_unused_registrations_expire_after_a_day(anonymous: httpx.AsyncClient) -> None:
    client = await register_client(anonymous)
    client_id = str(client["client_id"])
    assert await clients.get_client(client_id) is not None
    await age(client_id, created=timedelta(hours=23))
    assert await clients.get_client(client_id) is not None
    await age(client_id, created=timedelta(hours=25))
    assert await clients.get_client(client_id) is None
    # The next registration removes the expired row.
    await register_client(anonymous)
    async with session() as db:
        remaining = (await db.execute(select(OAuthClient.client_id))).scalars().all()
    assert client_id not in remaining


async def test_used_registrations_expire_after_ninety_idle_days(
    anonymous: httpx.AsyncClient,
) -> None:
    client_id = str((await register_client(anonymous))["client_id"])
    await age(client_id, created=timedelta(days=200), used=timedelta(days=89))
    assert await clients.get_client(client_id) is not None
    await age(client_id, created=timedelta(days=200), used=timedelta(days=91))
    assert await clients.get_client(client_id) is None


async def test_a_malformed_client_id_is_not_looked_up() -> None:
    for value in ("", "short", "has space in it 0123456789", "x" * 200, "a/b" * 10):
        assert await clients.get_client(value) is None


async def age(client_id: str, *, created: timedelta, used: timedelta | None = None) -> None:
    now = datetime.now(UTC)
    async with write_transaction() as db:
        await db.execute(
            update(OAuthClient)
            .where(OAuthClient.client_id == client_id)
            .values(created_at=now - created, last_used_at=None if used is None else now - used)
        )
