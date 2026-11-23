import asyncio
from datetime import timedelta

import httpx
import pytest
import respx
from guesty_fakes import FakeGuestyAuth, fixture_json, make_jwt
from pms_helpers import (
    ACCOUNT_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    make_connection,
    put_token,
    read_connection,
)

from pmsbridge.pms.base import PMSAuthError, PMSPermissionError
from pmsbridge.pms.guesty.auth import GuestyCredentials, GuestyTokenManager
from pmsbridge.pms.guesty.client import GuestyClient, _throttles, throttle_for
from pmsbridge.pms.http import RateLimits, Throttle

LISTINGS = "https://open-api.guesty.com/v1/listings"


async def make_client(account_id: str | None = ACCOUNT_ID) -> tuple[GuestyClient, str]:
    client_id = CLIENT_ID if account_id == ACCOUNT_ID else f"0cid-for-{account_id}"
    connection_id = await make_connection(
        f"Main {account_id}", client_id=client_id, account_id=account_id
    )
    manager = GuestyTokenManager(
        connection_id, GuestyCredentials(client_id, CLIENT_SECRET), account_id
    )
    return GuestyClient(manager, connection_id), connection_id


def bearer(route: respx.Route, index: int = -1) -> str:
    return route.calls[index].request.headers["authorization"]


async def test_requests_carry_the_stored_token_and_cost_one_token_request(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    route = mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(200, json={"results": []}))
    client, _ = await make_client()
    for _ in range(5):
        response = await client.request("GET", "/listings", params={"limit": 1})
        assert response.json() == {"results": []}
    assert fake_auth.requests == 1
    assert bearer(route).startswith("Bearer ") and route.call_count == 5


async def test_concurrent_requests_share_one_token_request(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(200, json={}))
    client, _ = await make_client()
    await asyncio.gather(*(client.request("GET", "/listings") for _ in range(20)))
    assert fake_auth.requests == 1


async def test_a_403_with_a_valid_token_does_not_refresh(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    route = mock_guesty.get(LISTINGS).mock(
        return_value=httpx.Response(403, json=fixture_json("permission_denied.json"))
    )
    client, _ = await make_client()
    with pytest.raises(PMSPermissionError):
        await client.request("GET", "/listings")
    assert fake_auth.requests == 1 and route.call_count == 1


async def due_but_kept(account_id: str = ACCOUNT_ID) -> tuple[GuestyClient, str, str]:
    """A client whose stored token is inside the refresh window but kept because the budget is low.

    That is the state in which a rejection from Guesty may call for a refresh.
    """
    client, connection_id = await make_client(account_id)
    token = make_jwt(CLIENT_ID, account_id)
    await put_token(
        connection_id,
        token,
        expires_in=timedelta(minutes=20),
        remaining=1,
        reset_in=timedelta(hours=5),
    )
    return client, connection_id, token


async def test_a_403_with_another_message_is_a_permission_error_without_refresh(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    route = mock_guesty.get(LISTINGS).mock(
        return_value=httpx.Response(403, json={"error": "Forbidden"})
    )
    client, _, _ = await due_but_kept()
    with pytest.raises(PMSPermissionError):
        await client.request("GET", "/listings")
    assert route.call_count == 1 and fake_auth.requests == 0


@pytest.mark.parametrize(
    "rejection",
    [
        httpx.Response(401),
        httpx.Response(403, json=fixture_json("permission_denied.json")),
    ],
)
async def test_a_rejection_of_a_due_token_refreshes_and_retries_once(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth, rejection: httpx.Response
) -> None:
    route = mock_guesty.get(LISTINGS).mock(side_effect=[rejection, httpx.Response(200, json={})])
    client, _, old = await due_but_kept()
    response = await client.request("GET", "/listings")
    assert response.status_code == 200
    assert fake_auth.requests == 1 and route.call_count == 2
    assert bearer(route, 0) == f"Bearer {old}" and bearer(route, 1) != bearer(route, 0)


async def test_a_second_rejection_gives_up_and_marks_the_connection(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    route = mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(401))
    client, connection_id, _ = await due_but_kept()
    with pytest.raises(PMSAuthError):
        await client.request("GET", "/listings")
    assert route.call_count == 2 and fake_auth.requests == 1  # never a second refresh
    assert (await read_connection(connection_id)).status == "needs_attention"


async def test_a_rejection_of_a_comfortably_valid_token_is_a_permission_error(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    route = mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(401))
    client, _ = await make_client()
    with pytest.raises(PMSPermissionError):
        await client.request("GET", "/listings")
    assert fake_auth.requests == 1 and route.call_count == 1


async def test_a_write_rejected_for_an_expired_token_is_not_resent(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    route = mock_guesty.post(LISTINGS).mock(
        side_effect=[httpx.Response(401), httpx.Response(201, json={"id": "x"})]
    )
    client, _, _ = await due_but_kept()
    with pytest.raises(PMSAuthError) as error:
        await client.request("POST", "/listings", json_body={"a": 1})
    assert route.call_count == 1 and fake_auth.requests == 1  # renewed once, not resent
    assert "Nothing was changed" in error.value.message
    # The renewed token is stored, so the caller's next attempt just works.
    response = await client.request("POST", "/listings", json_body={"a": 1})
    assert response.status_code == 201 and fake_auth.requests == 1


async def test_rate_limit_headers_from_guesty_slow_the_next_request(
    mock_guesty: respx.MockRouter, fake_auth: FakeGuestyAuth
) -> None:
    mock_guesty.get(LISTINGS).mock(
        side_effect=[
            httpx.Response(200, headers={"X-RateLimit-Remaining-Second": "0"}),
            httpx.Response(200),
        ]
    )
    client, _ = await make_client()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(seconds)

    _throttles[client.account_key] = Throttle(RateLimits(15, 120, 5000, 8), sleep=fake_sleep)
    await client.request("GET", "/listings")
    await client.request("GET", "/listings")
    assert len(slept) == 1 and 0 < slept[0] <= 1 / 15 + 0.01


async def test_one_throttle_is_shared_per_account() -> None:
    assert throttle_for("acct-x") is throttle_for("acct-x")
    first, _ = await make_client("acct-x")
    assert first.account_key == "acct-x"
    unknown, connection_id = await make_client(None)
    assert unknown.account_key == f"connection:{connection_id}"
