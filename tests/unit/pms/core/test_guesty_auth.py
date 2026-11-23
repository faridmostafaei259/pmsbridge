import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from guesty_fakes import FakeGuestyAuth, make_jwt
from pms_helpers import (
    ACCOUNT_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    make_connection,
    put_token,
    read_connection,
    read_token_row,
    set_expiry,
)
from sqlalchemy.exc import IntegrityError

import pmsbridge
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.pms.base import PMSAuthError, PMSTokenBudgetError, PMSUpstreamError
from pmsbridge.pms.db import PMSConnection, PMSToken, open_credentials, open_token
from pmsbridge.pms.guesty.auth import (
    GuestyCredentials,
    GuestyTokenManager,
    decode_jwt_claims,
    reset_token_state,
    store_issued_token,
    validate_credentials,
)

CREDENTIALS = GuestyCredentials(CLIENT_ID, CLIENT_SECRET)
SECOND_SECRET = "second-secret"  # noqa: S105  (invented)
WRONG_SECRET = "wrong-secret"  # noqa: S105  (invented)


def manager(connection_id: str) -> GuestyTokenManager:
    return GuestyTokenManager(connection_id, CREDENTIALS)


async def expire(connection_id: str) -> None:
    await set_expiry(connection_id, timedelta(minutes=-1))


# ---- one token per connection ----


async def test_a_new_connection_gets_one_token_and_reuses_it(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection()
    token = await manager(connection_id).get_token()
    assert decode_jwt_claims(token) is not None
    assert await manager(connection_id).get_token() == token
    assert fake_auth.requests == 1
    row = await read_token_row(connection_id)
    assert row is not None and row.budget_remaining == 4
    assert row.expires_at - datetime.now(UTC) > timedelta(hours=23)
    assert (await read_connection(connection_id)).guesty_account_id == ACCOUNT_ID


async def test_concurrent_callers_cause_exactly_one_token_request(
    fake_auth: FakeGuestyAuth,
) -> None:
    connection_id = await make_connection()
    tokens = await asyncio.gather(*(manager(connection_id).get_token() for _ in range(25)))
    assert len(set(tokens)) == 1
    assert fake_auth.requests == 1


async def test_a_restart_reuses_the_stored_token(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection()
    first = await manager(connection_id).get_token()
    # A restart loses all in-memory state: new manager, no locks, no blocks.
    reset_token_state()
    assert await manager(connection_id).get_token() == first
    assert fake_auth.requests == 1


async def test_the_token_is_stored_encrypted(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection()
    token = await manager(connection_id).get_token()
    row = await read_token_row(connection_id)
    assert row is not None
    assert token.encode() not in row.access_token
    assert open_token(row) == token
    conn = await read_connection(connection_id)
    assert CLIENT_SECRET.encode() not in conn.credentials
    assert open_credentials(conn)["client_id"] == CLIENT_ID


# ---- refresh window ----


@pytest.mark.parametrize(
    ("left", "refreshes"),
    [(timedelta(minutes=50), False), (timedelta(minutes=44), True), (timedelta(minutes=-1), True)],
)
async def test_refresh_window_default(
    fake_auth: FakeGuestyAuth, left: timedelta, refreshes: bool
) -> None:
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    await put_token(connection_id, make_jwt(CLIENT_ID, ACCOUNT_ID), expires_in=left)
    await manager(connection_id).get_token()
    assert fake_auth.requests == (1 if refreshes else 0)


async def test_the_refresh_window_is_configurable(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUESTY_TOKEN_REFRESH_MINUTES", "30")
    clear_settings_cache()
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    await put_token(
        connection_id, make_jwt(CLIENT_ID, ACCOUNT_ID), expires_in=timedelta(minutes=40)
    )
    await manager(connection_id).get_token()
    assert fake_auth.requests == 0
    await set_expiry(connection_id, timedelta(minutes=29))
    await manager(connection_id).get_token()
    assert fake_auth.requests == 1


# ---- budget ----


async def test_the_budget_is_tracked_and_marked_low(
    fake_auth: FakeGuestyAuth, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    connection_id = await make_connection()
    remaining: list[int | None] = []
    for _ in range(4):
        await manager(connection_id).get_token()
        row = await read_token_row(connection_id)
        assert row is not None
        remaining.append(row.budget_remaining)
        assert row.budget_reset_at is not None
        await expire(connection_id)
    assert remaining == [4, 3, 2, 1]
    assert (await read_connection(connection_id)).status == "token_budget_low"
    assert any("budget is low" in record.getMessage() for record in caplog.records)


async def test_a_low_budget_keeps_a_valid_token_instead_of_refreshing_early(
    fake_auth: FakeGuestyAuth,
) -> None:
    connection_id = await make_connection(account_id=ACCOUNT_ID, status="token_budget_low")
    token = make_jwt(CLIENT_ID, ACCOUNT_ID)
    await put_token(
        connection_id,
        token,
        expires_in=timedelta(minutes=20),
        remaining=1,
        reset_in=timedelta(hours=5),
    )
    assert await manager(connection_id).get_token() == token
    assert fake_auth.requests == 0
    await set_expiry(connection_id, timedelta(seconds=30))
    await manager(connection_id).get_token()
    assert fake_auth.requests == 1


async def test_a_recovered_budget_clears_the_low_status(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection(status="token_budget_low")
    await manager(connection_id).get_token()
    assert (await read_connection(connection_id)).status == "active"


@pytest.mark.parametrize("status", [429, 403, 500])
async def test_an_exhausted_budget_raises_without_retrying(
    fake_auth: FakeGuestyAuth, status: int
) -> None:
    fake_auth.exhausted_status = status
    connection_id = await make_connection()
    await manager(connection_id).get_token()
    fake_auth.limit = 1  # Guesty now counts the first token as the last one
    await expire(connection_id)
    with pytest.raises(PMSTokenBudgetError) as error:
        await manager(connection_id).get_token()
    assert "5 access tokens per 24 hours" in error.value.message
    assert "minutes" in error.value.message
    assert fake_auth.requests == 2  # one success, one refused: no retry
    # Further calls do not even ask.
    with pytest.raises(PMSTokenBudgetError):
        await manager(connection_id).get_token()
    assert fake_auth.requests == 2
    row = await read_token_row(connection_id)
    assert row is not None and row.budget_remaining == 0
    assert (await read_connection(connection_id)).status == "token_budget_low"


async def test_a_stored_empty_budget_prevents_any_request(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    await put_token(
        connection_id,
        make_jwt(CLIENT_ID, ACCOUNT_ID),
        expires_in=timedelta(minutes=-1),
        remaining=0,
        reset_in=timedelta(hours=3),
    )
    with pytest.raises(PMSTokenBudgetError) as error:
        await manager(connection_id).get_token()
    assert "about 180 minutes" in error.value.message
    assert fake_auth.requests == 0


async def test_a_new_connection_with_no_budget_asks_once_then_stops(
    fake_auth: FakeGuestyAuth,
) -> None:
    fake_auth.limit = 0
    connection_id = await make_connection()
    for _ in range(3):
        with pytest.raises(PMSTokenBudgetError):
            await manager(connection_id).get_token()
    assert fake_auth.requests == 1


# ---- identity checks ----


async def test_a_token_for_another_client_id_is_refused(
    fake_auth: FakeGuestyAuth, caplog: pytest.LogCaptureFixture
) -> None:
    fake_auth.override_cid = "someone-elses-client"
    connection_id = await make_connection()
    with pytest.raises(PMSAuthError):
        await manager(connection_id).get_token()
    assert await read_token_row(connection_id) is None
    assert (await read_connection(connection_id)).status == "needs_attention"
    assert any("security event" in record.getMessage() for record in caplog.records)


async def test_an_account_change_is_refused(
    fake_auth: FakeGuestyAuth, caplog: pytest.LogCaptureFixture
) -> None:
    connection_id = await make_connection()
    old = await manager(connection_id).get_token()
    await expire(connection_id)
    fake_auth.override_account = "another-account"
    with pytest.raises(PMSAuthError):
        await manager(connection_id).get_token()
    conn = await read_connection(connection_id)
    assert conn.guesty_account_id == ACCOUNT_ID
    assert conn.status == "needs_attention"
    row = await read_token_row(connection_id)
    assert row is not None and open_token(row) == old
    assert any("security event" in record.getMessage() for record in caplog.records)


async def test_an_account_already_connected_is_refused(fake_auth: FakeGuestyAuth) -> None:
    fake_auth.add_account("0second-client-id-9999", SECOND_SECRET, ACCOUNT_ID)
    first = await make_connection("First")
    second = await make_connection(
        "Second", client_id="0second-client-id-9999", secret=SECOND_SECRET
    )
    await manager(first).get_token()
    other = GuestyTokenManager(second, GuestyCredentials("0second-client-id-9999", SECOND_SECRET))
    with pytest.raises(PMSAuthError) as error:
        await other.get_token()
    assert "already connected" in error.value.message
    assert (await read_connection(second)).guesty_account_id is None
    assert await read_token_row(second) is None


async def test_the_database_refuses_a_duplicate_account_id() -> None:
    await make_connection("First", account_id=ACCOUNT_ID)
    with pytest.raises(IntegrityError):
        await make_connection("Second", client_id="other-client-0000", account_id=ACCOUNT_ID)


# ---- failures ----


async def test_bad_credentials_mark_the_connection_and_are_not_retried(
    fake_auth: FakeGuestyAuth,
) -> None:
    connection_id = await make_connection(secret=WRONG_SECRET)
    bad = GuestyTokenManager(connection_id, GuestyCredentials(CLIENT_ID, WRONG_SECRET))
    with pytest.raises(PMSAuthError):
        await bad.get_token()
    assert fake_auth.requests == 1
    assert (await read_connection(connection_id)).status == "needs_attention"


async def test_a_network_error_is_not_retried(fake_auth: FakeGuestyAuth) -> None:
    fake_auth.mode = "network"
    connection_id = await make_connection()
    with pytest.raises(PMSUpstreamError) as error:
        await manager(connection_id).get_token()
    assert "not repeated" in error.value.message
    assert fake_auth.requests == 1


@pytest.mark.parametrize("mode", ["server_error", "garbage"])
async def test_server_errors_and_unreadable_tokens_are_not_retried(
    fake_auth: FakeGuestyAuth, mode: str
) -> None:
    fake_auth.mode = mode
    connection_id = await make_connection()
    with pytest.raises(PMSUpstreamError):
        await manager(connection_id).get_token()
    assert fake_auth.requests == 1
    assert await read_token_row(connection_id) is None


async def test_a_disabled_connection_gets_no_token(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection(status="disabled")
    with pytest.raises(PMSAuthError):
        await manager(connection_id).get_token()
    assert fake_auth.requests == 0


# ---- expiry rejections ----


async def test_a_rejection_with_a_valid_token_does_not_refresh(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection()
    token = await manager(connection_id).get_token()
    assert await manager(connection_id).refresh_after_rejection(token) is None
    assert fake_auth.requests == 1


@pytest.mark.parametrize("left", [timedelta(minutes=10), timedelta(minutes=-5)])
async def test_a_rejection_of_a_due_token_refreshes_once(
    fake_auth: FakeGuestyAuth, left: timedelta
) -> None:
    connection_id = await make_connection()
    token = await manager(connection_id).get_token()
    await set_expiry(connection_id, left)
    fresh = await manager(connection_id).refresh_after_rejection(token)
    assert fresh is not None and fresh != token
    assert fake_auth.requests == 2
    # A second rejection of the old token reuses the one already refreshed.
    assert await manager(connection_id).refresh_after_rejection(token) == fresh
    assert fake_auth.requests == 2


async def test_a_rejection_uses_the_jwt_expiry_too(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    short = make_jwt(CLIENT_ID, ACCOUNT_ID, expires_in=60)
    await put_token(connection_id, short, expires_in=timedelta(hours=20))
    assert await manager(connection_id).refresh_after_rejection(short) is not None
    assert fake_auth.requests == 1


# ---- connecting ----


async def test_validation_returns_the_token_so_it_can_be_kept(fake_auth: FakeGuestyAuth) -> None:
    issued = await validate_credentials(CLIENT_ID, CLIENT_SECRET)
    assert issued.account_id == ACCOUNT_ID and issued.budget_remaining == 4
    assert CLIENT_SECRET not in repr(issued) and issued.access_token not in repr(issued)
    connection_id = await make_connection()
    async with write_transaction() as db:
        conn = await db.get(PMSConnection, connection_id)
        assert conn is not None
        assert await store_issued_token(db, conn, issued) is None
    # The validation token is the connection's token: no second request.
    assert await manager(connection_id).get_token() == issued.access_token
    assert fake_auth.requests == 1
    assert (await read_connection(connection_id)).guesty_account_id == ACCOUNT_ID


async def test_validation_reports_bad_credentials_and_exhausted_budget(
    fake_auth: FakeGuestyAuth,
) -> None:
    with pytest.raises(PMSAuthError):
        await validate_credentials(CLIENT_ID, "nope")
    fake_auth.limit = 0
    with pytest.raises(PMSTokenBudgetError):
        await validate_credentials(CLIENT_ID, CLIENT_SECRET)


async def test_a_duplicate_account_is_refused_when_connecting(fake_auth: FakeGuestyAuth) -> None:
    await make_connection("Existing", account_id=ACCOUNT_ID, client_id="0first-client-0000")
    issued = await validate_credentials(CLIENT_ID, CLIENT_SECRET)
    connection_id = await make_connection("Duplicate")
    async with write_transaction() as db:
        conn = await db.get(PMSConnection, connection_id)
        assert conn is not None
        assert isinstance(await store_issued_token(db, conn, issued), PMSAuthError)
    async with session() as db:
        assert await db.get(PMSToken, connection_id) is None


# ---- secrets never reach the logs ----


async def test_credentials_and_tokens_never_appear_in_logs(
    fake_auth: FakeGuestyAuth, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    connection_id = await make_connection()
    token = await manager(connection_id).get_token()
    for _ in range(3):
        await expire(connection_id)
        await manager(connection_id).get_token()
    fake_auth.limit = 4
    await expire(connection_id)
    with pytest.raises(PMSTokenBudgetError):
        await manager(connection_id).get_token()
    fake_auth.override_cid = "other"
    fake_auth.limit = 50
    fake_auth.add_account("0other-client-0001", CLIENT_SECRET, "acct-other")
    other = await make_connection("Other", client_id="0other-client-0001")
    with pytest.raises(PMSAuthError):
        await GuestyTokenManager(
            other, GuestyCredentials("0other-client-0001", CLIENT_SECRET)
        ).get_token()
    text = "\n".join(
        record.getMessage() + repr(record.__dict__.get("extra", "")) for record in caplog.records
    )
    full = "\n".join(str(record.__dict__) for record in caplog.records)
    for secret in (CLIENT_SECRET, token, CLIENT_ID):
        assert secret not in text and secret not in full


async def test_the_token_endpoint_is_only_called_from_the_auth_module(
    mock_guesty: respx.MockRouter,
) -> None:
    root = Path(pmsbridge.__file__).parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "oauth2/token" in path.read_text()
        and path.relative_to(root).as_posix() != "pms/guesty/auth.py"
    ]
    assert offenders == []
