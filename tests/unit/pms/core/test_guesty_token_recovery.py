"""Token safety: an unsaved token, a token request that hangs, the caller's deadline,
and credential replacement under the connection lock."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from guesty_fakes import FakeGuestyAuth, make_jwt
from pms_helpers import (
    ACCOUNT_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    make_connection,
    read_connection,
    read_token_row,
)
from sqlalchemy.exc import OperationalError

from pmsbridge.core.storage import write_transaction
from pmsbridge.pms.base import PMSAuthError, PMSUpstreamError, call_deadline
from pmsbridge.pms.db import PMSConnection, open_credentials
from pmsbridge.pms.guesty import auth
from pmsbridge.pms.guesty.auth import GuestyCredentials, GuestyTokenManager, IssuedToken

CREDENTIALS = GuestyCredentials(CLIENT_ID, CLIENT_SECRET)


def manager(connection_id: str) -> GuestyTokenManager:
    return GuestyTokenManager(connection_id, CREDENTIALS)


def fail_writes(monkeypatch: pytest.MonkeyPatch, times: int | None) -> list[int]:
    """Make `write_transaction` in the token module fail `times` times (always when None)."""
    real = auth.write_transaction
    calls: list[int] = []

    def flaky() -> object:
        calls.append(1)
        if times is None or len(calls) <= times:
            raise OperationalError("insert", {}, Exception("disk I/O error: secret detail"))
        return real()

    monkeypatch.setattr(auth, "write_transaction", flaky)
    return calls


# ---- a token that could not be stored ----


async def test_a_token_that_could_not_be_stored_is_kept_and_stored_on_the_next_call(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = await make_connection()
    fail_writes(monkeypatch, 1)
    with pytest.raises(PMSUpstreamError) as error:
        await manager(connection_id).get_token()
    assert "could not be saved" in error.value.message
    assert "disk" not in error.value.message and "secret detail" not in error.value.message
    assert connection_id in auth._unsaved
    assert connection_id in auth._cooldown
    assert await read_token_row(connection_id) is None
    issued = auth._unsaved[connection_id]
    token = await manager(connection_id).get_token()
    assert token == issued.access_token  # served from memory, and stored now
    assert connection_id not in auth._unsaved
    row = await read_token_row(connection_id)
    assert row is not None
    assert connection_id not in auth._cooldown
    assert await manager(connection_id).get_token() == token
    assert fake_auth.requests == 1  # exactly one token request in all


async def test_an_unsaved_token_is_used_while_storing_keeps_failing(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = await make_connection()
    fail_writes(monkeypatch, None)
    with pytest.raises(PMSUpstreamError):
        await manager(connection_id).get_token()
    issued = auth._unsaved[connection_id]
    for _ in range(3):
        assert await manager(connection_id).get_token() == issued.access_token
    assert fake_auth.requests == 1


async def test_an_unsaved_token_close_to_expiry_is_dropped_and_asks_nothing(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = await make_connection()
    fail_writes(monkeypatch, None)
    with pytest.raises(PMSUpstreamError):
        await manager(connection_id).get_token()
    auth._unsaved[connection_id] = replace(
        auth._unsaved[connection_id], expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    with pytest.raises(PMSUpstreamError) as error:
        await manager(connection_id).get_token()
    assert "paused" in error.value.message  # the cool-down, not a new token request
    assert connection_id not in auth._unsaved
    assert fake_auth.requests == 1


async def test_forgetting_a_connection_drops_its_unsaved_token(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = await make_connection()
    fail_writes(monkeypatch, 1)
    with pytest.raises(PMSUpstreamError):
        await manager(connection_id).get_token()
    auth.forget_connection(connection_id)
    assert connection_id not in auth._unsaved


# ---- the token request has a total time limit ----


async def test_a_token_request_that_never_finishes_ends_and_starts_the_cool_down(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("not reached")

    fake_auth.route.mock(side_effect=hang)
    monkeypatch.setattr(auth, "TOKEN_TOTAL_SECONDS", 0.2)
    connection_id = await make_connection()
    started = time.monotonic()
    with pytest.raises(PMSUpstreamError) as error:
        await manager(connection_id).get_token()
    assert time.monotonic() - started < 5
    assert "could not be reached" in error.value.message
    assert connection_id in auth._cooldown


# ---- the caller's deadline stays out of the refresh task ----


async def test_the_refresh_task_does_not_inherit_the_callers_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float | None] = []

    async def fake_request(credentials: GuestyCredentials) -> IssuedToken:
        seen.append(call_deadline.get())
        now = datetime.now(UTC)
        return IssuedToken(
            access_token=make_jwt(credentials.client_id, ACCOUNT_ID),
            expires_at=now + timedelta(days=1),
            obtained_at=now,
            account_id=ACCOUNT_ID,
            budget_remaining=4,
            budget_reset_at=None,
        )

    monkeypatch.setattr(auth, "_request_token", fake_request)
    connection_id = await make_connection()
    call_deadline.set(time.monotonic() + 30)
    try:
        await manager(connection_id).get_token()
    finally:
        call_deadline.set(None)
    assert seen == [None]


async def test_a_timeout_that_is_not_the_callers_is_not_called_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_credentials: GuestyCredentials) -> IssuedToken:
        raise TimeoutError

    monkeypatch.setattr(auth, "_request_token", broken)
    connection_id = await make_connection()
    with pytest.raises(PMSUpstreamError) as error:
        await manager(connection_id).get_token()
    assert "still being renewed" not in error.value.message


# ---- replacing credentials takes the lock ----


class Gate:
    def __init__(self, fake: FakeGuestyAuth) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.arrived = 0
        self.fake = fake
        fake.route.mock(side_effect=self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.arrived += 1
        self.started.set()
        await self.release.wait()
        return self.fake._handle(request)


async def test_replacing_credentials_waits_for_a_refresh_in_flight(
    fake_auth: FakeGuestyAuth,
) -> None:
    fake_auth.add_account("0new-client-id-5678", "new-secret-value", ACCOUNT_ID)
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    gate = Gate(fake_auth)
    refresh = asyncio.create_task(manager(connection_id).get_token())
    await asyncio.wait_for(gate.started.wait(), 5)
    swap = asyncio.create_task(
        auth.replace_credentials(connection_id, "0new-client-id-5678", "new-secret-value")
    )
    await asyncio.sleep(0.1)
    assert gate.arrived == 1  # the replacement has not asked yet
    assert not swap.done()
    gate.release.set()
    await asyncio.wait_for(refresh, 5)
    await asyncio.wait_for(swap, 5)
    assert fake_auth.requests == 2
    assert fake_auth.used("0new-client-id-5678") == 1


async def test_replacing_credentials_swaps_them_with_the_token_and_keeps_the_id(
    fake_auth: FakeGuestyAuth,
) -> None:
    fake_auth.add_account("0new-client-id-5678", "new-secret-value", ACCOUNT_ID)
    connection_id = await make_connection(status="needs_attention", account_id=ACCOUNT_ID)
    auth._cooldown[connection_id] = (datetime.now(UTC) + timedelta(minutes=5), timedelta(minutes=1))
    await auth.replace_credentials(connection_id, "0new-client-id-5678", "new-secret-value")
    conn = await read_connection(connection_id)
    assert conn.id == connection_id
    assert conn.status == "active"
    assert conn.client_id_hint == "5678"
    assert open_credentials(conn) == {
        "client_id": "0new-client-id-5678",
        "client_secret": "new-secret-value",
    }
    assert await read_token_row(connection_id) is not None
    assert connection_id not in auth._cooldown
    assert fake_auth.requests == 1


async def test_credentials_that_guesty_refuses_leave_the_old_ones_in_place(
    fake_auth: FakeGuestyAuth,
) -> None:
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    with pytest.raises(PMSAuthError):
        await auth.replace_credentials(connection_id, CLIENT_ID, "wrong-secret")
    conn = await read_connection(connection_id)
    assert open_credentials(conn)["client_secret"] == CLIENT_SECRET
    assert conn.status == "active"


async def test_credentials_for_another_account_are_refused_and_change_nothing(
    fake_auth: FakeGuestyAuth,
) -> None:
    fake_auth.add_account("0other-client-id-9999", "other-secret", "another-account")
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    with pytest.raises(PMSAuthError):
        await auth.replace_credentials(connection_id, "0other-client-id-9999", "other-secret")
    conn = await read_connection(connection_id)
    assert open_credentials(conn)["client_id"] == CLIENT_ID
    assert conn.guesty_account_id == ACCOUNT_ID


async def test_replacing_credentials_with_a_key_used_elsewhere_is_refused(
    fake_auth: FakeGuestyAuth,
) -> None:
    await make_connection("Other", client_id="0other-client-id-9999", account_id="another-account")
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    fake_auth.add_account("0other-client-id-9999", "other-secret", ACCOUNT_ID)
    with pytest.raises(PMSAuthError):
        await auth.replace_credentials(connection_id, "0other-client-id-9999", "other-secret")
    async with write_transaction() as db:
        conn = await db.get(PMSConnection, connection_id)
        assert conn is not None
        assert open_credentials(conn)["client_id"] == CLIENT_ID
