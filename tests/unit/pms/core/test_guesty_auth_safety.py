"""Token safety: cool-down, needs-attention gating, cancellation and no transaction held."""

import ast
import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from guesty_fakes import FakeGuestyAuth
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
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.pms.base import (
    PMSAuthError,
    PMSTokenBudgetError,
    PMSUpstreamError,
    call_deadline,
)
from pmsbridge.pms.db import PMSConnection, client_id_in_use
from pmsbridge.pms.guesty import auth
from pmsbridge.pms.guesty.auth import GuestyCredentials, GuestyTokenManager, forget_connection

CREDENTIALS = GuestyCredentials(CLIENT_ID, CLIENT_SECRET)


def manager(connection_id: str) -> GuestyTokenManager:
    return GuestyTokenManager(connection_id, CREDENTIALS)


def expire_cooldown(connection_id: str) -> None:
    until, delay = auth._cooldown[connection_id]
    auth._cooldown[connection_id] = (until - delay - timedelta(seconds=1), delay)


# ---- needs_attention gating ----


@pytest.mark.parametrize("problem", ["cid", "account", "credentials"])
async def test_a_connection_that_needs_attention_never_asks_again(
    fake_auth: FakeGuestyAuth, problem: str
) -> None:
    connection_id = await make_connection()
    creds = CREDENTIALS
    if problem == "cid":
        fake_auth.override_cid = "someone-elses-client"
    elif problem == "account":
        await manager(connection_id).get_token()
        await set_expiry(connection_id, timedelta(minutes=-1))
        fake_auth.override_account = "another-account"
        fake_auth.requests = 0
    else:
        creds = GuestyCredentials(CLIENT_ID, "wrong")
    subject = GuestyTokenManager(connection_id, creds)
    with pytest.raises(PMSAuthError):
        await subject.get_token()
    assert (await read_connection(connection_id)).status == "needs_attention"
    asked = fake_auth.requests
    for _ in range(2):
        with pytest.raises(PMSAuthError) as error:
            await subject.get_token()
        assert "needs attention" in error.value.message
    assert fake_auth.requests == asked


async def test_a_rejection_on_a_connection_needing_attention_asks_nothing(
    fake_auth: FakeGuestyAuth,
) -> None:
    connection_id = await make_connection(status="needs_attention", account_id=ACCOUNT_ID)
    with pytest.raises(PMSAuthError):
        await manager(connection_id).refresh_after_rejection("a.b.c")
    assert fake_auth.requests == 0


# ---- cool-down ----


@pytest.mark.parametrize("mode", ["server_error", "garbage", "network"])
async def test_a_failed_token_request_starts_a_cooldown(
    fake_auth: FakeGuestyAuth, mode: str
) -> None:
    fake_auth.mode = mode
    connection_id = await make_connection()
    with pytest.raises(PMSUpstreamError):
        await manager(connection_id).get_token()
    for _ in range(3):
        with pytest.raises(PMSUpstreamError) as error:
            await manager(connection_id).get_token()
        assert "paused" in error.value.message and "seconds" in error.value.message
    assert fake_auth.requests == 1


async def test_the_cooldown_doubles_up_to_a_cap_and_clears_on_success(
    fake_auth: FakeGuestyAuth,
) -> None:
    fake_auth.mode = "server_error"
    connection_id = await make_connection()
    delays: list[int] = []
    for _ in range(7):
        with pytest.raises(PMSUpstreamError):
            await manager(connection_id).get_token()
        delays.append(int(auth._cooldown[connection_id][1].total_seconds()))
        expire_cooldown(connection_id)
    assert delays == [60, 120, 240, 480, 900, 900, 900]
    fake_auth.mode = None
    await manager(connection_id).get_token()
    assert connection_id not in auth._cooldown


async def test_a_cooldown_keeps_a_still_valid_token_in_use(fake_auth: FakeGuestyAuth) -> None:
    connection_id = await make_connection(account_id=ACCOUNT_ID)
    fake_auth.mode = "server_error"
    await put_token(connection_id, "a.b.c", expires_in=timedelta(minutes=-1))
    with pytest.raises(PMSUpstreamError):
        await manager(connection_id).get_token()
    await put_token(connection_id, "d.e.f", expires_in=timedelta(minutes=20))
    assert await manager(connection_id).get_token() == "d.e.f"
    assert fake_auth.requests == 1


async def test_an_empty_budget_in_a_response_blocks_even_without_a_stored_token(
    fake_auth: FakeGuestyAuth,
) -> None:
    fake_auth.limit = 0
    connection_id = await make_connection()
    with pytest.raises(PMSTokenBudgetError):
        await manager(connection_id).get_token()
    assert connection_id in auth._blocked_until
    with pytest.raises(PMSTokenBudgetError):
        await manager(connection_id).get_token()
    assert fake_auth.requests == 1


async def test_taking_the_last_token_blocks_the_next_refresh(fake_auth: FakeGuestyAuth) -> None:
    fake_auth.limit = 1
    connection_id = await make_connection()
    await manager(connection_id).get_token()
    assert connection_id in auth._blocked_until
    await set_expiry(connection_id, timedelta(minutes=-1))
    with pytest.raises(PMSTokenBudgetError):
        await manager(connection_id).get_token()
    assert fake_auth.requests == 1


async def test_forgetting_a_connection_clears_only_its_blocks(fake_auth: FakeGuestyAuth) -> None:
    fake_auth.mode = "server_error"
    first = await make_connection("One")
    second = await make_connection("Two", client_id="0two-client-id-0000")
    for connection_id in (first, second):
        with pytest.raises(PMSUpstreamError):
            await GuestyTokenManager(connection_id, CREDENTIALS).get_token()
    auth._blocked_until[first] = datetime.now(UTC) + timedelta(hours=1)
    lock = auth._locks.setdefault(first, asyncio.Lock())
    forget_connection(first)
    assert first not in auth._cooldown and first not in auth._blocked_until
    assert second in auth._cooldown
    assert auth._locks[first] is lock


# ---- no transaction across the call, and cancellation ----


class Gate:
    """Holds the token response until released, so a test can act while the request is open."""

    def __init__(self, fake: FakeGuestyAuth) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fake = fake
        fake.route.mock(side_effect=self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        await self.release.wait()
        return self.fake._handle(request)


async def wait_for_refresh_to_end() -> None:
    tasks = list(auth._tasks.values())
    if tasks:
        await asyncio.wait(tasks, timeout=5)
    await asyncio.sleep(0)  # let the task's done callback run


async def test_no_database_transaction_is_open_during_the_token_request(
    fake_auth: FakeGuestyAuth,
) -> None:
    gate = Gate(fake_auth)
    first = await make_connection("One")
    second = await make_connection("Two", client_id="0two-client-id-0000")
    caller = asyncio.create_task(manager(first).get_token())
    await asyncio.wait_for(gate.started.wait(), 5)
    started = time.monotonic()
    async with write_transaction() as db:  # would wait for the busy timeout if one were held
        other = await db.get(PMSConnection, second)
        assert other is not None
        other.label = "Renamed"
    assert time.monotonic() - started < 1
    gate.release.set()
    await caller


async def test_a_cancelled_caller_does_not_lose_the_token(fake_auth: FakeGuestyAuth) -> None:
    gate = Gate(fake_auth)
    connection_id = await make_connection()
    caller = asyncio.create_task(manager(connection_id).get_token())
    await asyncio.wait_for(gate.started.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # Another caller arrives while the request is still open: it waits, it does not ask again.
    second = asyncio.create_task(manager(connection_id).get_token())
    await asyncio.sleep(0.05)
    gate.release.set()
    token = await asyncio.wait_for(second, 5)
    await wait_for_refresh_to_end()
    assert fake_auth.requests == 1
    row = await read_token_row(connection_id)
    assert row is not None
    assert await manager(connection_id).get_token() == token
    assert fake_auth.requests == 1


async def test_a_caller_out_of_time_gets_an_error_and_the_token_is_still_stored(
    fake_auth: FakeGuestyAuth,
) -> None:
    gate = Gate(fake_auth)
    connection_id = await make_connection()
    call_deadline.set(time.monotonic() + 0.1)
    try:
        with pytest.raises(PMSUpstreamError) as error:
            await manager(connection_id).get_token()
    finally:
        call_deadline.set(None)
    assert "still being renewed" in error.value.message
    gate.release.set()
    await wait_for_refresh_to_end()
    assert await read_token_row(connection_id) is not None
    await manager(connection_id).get_token()
    assert fake_auth.requests == 1


async def test_a_connection_disabled_during_the_request_gets_no_token(
    fake_auth: FakeGuestyAuth,
) -> None:
    gate = Gate(fake_auth)
    connection_id = await make_connection()
    caller = asyncio.create_task(manager(connection_id).get_token())
    await asyncio.wait_for(gate.started.wait(), 5)
    async with write_transaction() as db:
        conn = await db.get(PMSConnection, connection_id)
        assert conn is not None
        conn.status = "disabled"
    gate.release.set()
    with pytest.raises(PMSAuthError):
        await caller
    assert await read_token_row(connection_id) is None


async def test_a_unique_constraint_failure_while_storing_needs_attention(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def clash(*_args: object) -> None:
        raise IntegrityError("insert", {}, Exception("unique"))

    monkeypatch.setattr(auth, "store_issued_token", clash)
    connection_id = await make_connection()
    with pytest.raises(PMSAuthError):
        await manager(connection_id).get_token()
    assert (await read_connection(connection_id)).status == "needs_attention"


# ---- duplicate client id ----


async def test_a_duplicate_client_id_is_found_before_any_token_is_requested(
    fake_auth: FakeGuestyAuth,
) -> None:
    existing = await make_connection("First")
    async with session() as db:
        assert await client_id_in_use(db, CLIENT_ID)
        assert not await client_id_in_use(db, CLIENT_ID, except_connection_id=existing)
        assert not await client_id_in_use(db, "0never-used-client")
    assert fake_auth.requests == 0
    with pytest.raises(IntegrityError):  # and the database refuses it regardless
        await make_connection("Second")


# ---- who may call the token endpoint ----

ROOT = Path(pmsbridge.__file__).parent
AUTH_MODULE = "pms/guesty/auth.py"
PRIVATE_NAMES = {"TOKEN_URL", "_request_token"}
VALIDATE_ALLOWED = {"pms/connections.py"}


def _uses(tree: ast.AST, names: set[str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found |= {alias.name for alias in node.names} & names
        elif isinstance(node, ast.Attribute) and node.attr in names:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in names:
            found.add(node.id)
    return found


def test_only_the_auth_module_touches_the_token_endpoint() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text()
        if relative != AUTH_MODULE and "oauth2/token" in source:
            offenders.append(f"{relative}: address")
        tree = ast.parse(source)
        if relative != AUTH_MODULE:
            offenders += [f"{relative}: {name}" for name in _uses(tree, PRIVATE_NAMES)]
        if relative != AUTH_MODULE and relative not in VALIDATE_ALLOWED:
            offenders += [f"{relative}: {n}" for n in _uses(tree, {"validate_credentials"})]
    assert offenders == []
