"""The per-approval rate limit: buckets, memory bound, and what an assistant sees when it is hit."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from framework_tools import Spy, add_tools, make_loader
from mcp_helpers import call_tool, mint_token, result_text, seed_connection, seed_user
from sqlalchemy import select

from pmsbridge.core.audit import AuditLog
from pmsbridge.core.storage import session
from pmsbridge.mcp import framework
from pmsbridge.mcp.framework import RateLimiter
from pmsbridge.pms import registry
from pmsbridge.pms.base import Provider


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_a_burst_is_allowed_then_refused_with_the_time_to_wait(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(framework.CALLS_BURST):
        assert limiter.check(1, write=False).allowed
    refused = limiter.check(1, write=False)
    assert not refused.allowed
    assert refused.wait_seconds == pytest.approx(1.0)


def test_calls_come_back_at_sixty_a_minute(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(framework.CALLS_BURST):
        limiter.check(1, write=False)
    clock.now += 0.5
    assert not limiter.check(1, write=False).allowed
    clock.now += 0.5
    assert limiter.check(1, write=False).allowed
    assert not limiter.check(1, write=False).allowed
    clock.now += 10
    allowed = sum(limiter.check(1, write=False).allowed for _ in range(30))
    assert allowed == 10


def test_a_refused_call_uses_nothing_so_waiting_is_enough(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(framework.CALLS_BURST):
        limiter.check(1, write=False)
    for _ in range(1000):
        assert not limiter.check(1, write=False).allowed
    clock.now += 1.0
    assert limiter.check(1, write=False).allowed


def test_changes_are_limited_separately_and_more_tightly(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(framework.WRITES_BURST):
        assert limiter.check(1, write=True).allowed
    refused = limiter.check(1, write=True)
    assert not refused.allowed
    assert refused.wait_seconds == pytest.approx(60 / framework.WRITES_PER_MINUTE)
    assert limiter.check(1, write=False).allowed
    clock.now += 6.0
    assert limiter.check(1, write=True).allowed


def test_changes_also_count_against_all_calls(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(framework.CALLS_BURST - framework.WRITES_BURST):
        assert limiter.check(1, write=False).allowed
    for _ in range(framework.WRITES_BURST):
        assert limiter.check(1, write=True).allowed
    assert not limiter.check(1, write=False).allowed


def test_approvals_are_limited_independently(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(framework.CALLS_BURST):
        limiter.check(1, write=False)
    assert not limiter.check(1, write=False).allowed
    assert limiter.check(2, write=False).allowed


def test_memory_is_bounded_and_idle_approvals_are_forgotten(clock: Clock) -> None:
    limiter = RateLimiter(max_grants=5, clock=clock)
    for grant in range(50):
        limiter.check(grant, write=False)
        assert limiter.tracked() <= 5
    assert limiter.tracked() == 5
    clock.now += framework.IDLE_FORGET_SECONDS + 1
    limiter.check(999, write=False)
    assert limiter.tracked() == 1


def test_the_oldest_approval_is_dropped_first(clock: Clock) -> None:
    limiter = RateLimiter(max_grants=2, clock=clock)
    for _ in range(framework.CALLS_BURST):
        limiter.check(1, write=False)
    limiter.check(2, write=False)
    limiter.check(3, write=False)
    # Grant 1 was dropped, so it starts again with a full burst.
    assert limiter.check(1, write=False).allowed


def test_a_flood_asks_for_one_audit_entry_a_minute(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)

    def flood() -> bool:
        decisions = [limiter.check(1, write=False) for _ in range(framework.CALLS_BURST + 5)]
        return any(decision.audit for decision in decisions)

    assert flood()
    clock.now += 30.0
    assert not flood()
    clock.now += 40.0
    assert flood()


@pytest.fixture
async def world(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> AsyncIterator[tuple[httpx.AsyncClient, str, Spy]]:
    spy = Spy()
    add_tools(app.state.mcp_server, spy)
    monkeypatch.setattr(framework, "_limiter", RateLimiter(clock=clock))
    user = await seed_user()
    connection = await seed_connection("Beach houses", writes_enabled=True)
    monkeypatch.setattr(
        registry,
        "load_provider",
        make_loader(spy, {connection: ("Beach houses", Provider.GUESTY)}),
    )
    token, _ = await mint_token(user, connections=(connection,))
    yield client, token, spy


async def audit_rows() -> list[AuditLog]:
    async with session() as db:
        return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


async def test_an_assistant_over_the_limit_is_told_how_long_to_wait(
    world: tuple[httpx.AsyncClient, str, Spy], clock: Clock
) -> None:
    client, token, spy = world
    for _ in range(framework.CALLS_BURST):
        assert (await call_tool(client, token, "t_read"))["isError"] is False
    refused = await call_tool(client, token, "t_read")
    assert refused["isError"] is True
    assert "requests too quickly" in result_text(refused)
    assert "Wait about 1 second, then" in result_text(refused)
    assert len(spy.calls) == framework.CALLS_BURST
    clock.now += 1.0
    assert (await call_tool(client, token, "t_read"))["isError"] is False


async def test_a_flood_of_refused_calls_leaves_one_audit_entry(
    world: tuple[httpx.AsyncClient, str, Spy],
) -> None:
    client, token, _ = world
    for _ in range(framework.CALLS_BURST + 15):
        await call_tool(client, token, "t_read")
    rows = await audit_rows()
    limited = [row for row in rows if row.error_class == "rate_limited"]
    assert len(limited) == 1
    assert (limited[0].status, limited[0].tool) == ("denied", "t_read")
    assert limited[0].connection_id is None
    assert len(rows) == framework.CALLS_BURST + 1


async def test_changes_hit_their_own_limit_while_reads_continue(
    world: tuple[httpx.AsyncClient, str, Spy],
) -> None:
    client, token, spy = world
    for _ in range(framework.WRITES_BURST):
        assert (await call_tool(client, token, "t_write", {"text": "hi"}))["isError"] is False
    refused = await call_tool(client, token, "t_write", {"text": "hi"})
    assert refused["isError"] is True and "changes too quickly" in result_text(refused)
    assert "Wait about 6 seconds, then" in result_text(refused)
    assert sum(call["tool"] == "t_write" for call in spy.calls) == framework.WRITES_BURST
    assert (await call_tool(client, token, "t_read"))["isError"] is False


async def test_the_limit_applies_before_the_connection_is_looked_up(
    world: tuple[httpx.AsyncClient, str, Spy],
) -> None:
    client, token, _ = world
    for _ in range(framework.CALLS_BURST):
        await call_tool(client, token, "list_connections")
    refused = await call_tool(client, token, "t_read", {"connection": "no-such-connection"})
    assert "too quickly" in result_text(refused)


async def test_changes_are_named_only_when_the_changes_limit_set_the_wait(
    world: tuple[httpx.AsyncClient, str, Spy],
) -> None:
    client, token, _ = world
    for _ in range(framework.CALLS_BURST):
        await call_tool(client, token, "t_read")
    refused = await call_tool(client, token, "t_write", {"text": "hi"})
    assert "requests too quickly" in result_text(refused)


def test_the_decision_says_which_limit_refused(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    assert limiter.check(1, write=True).bucket == ""
    for _ in range(framework.WRITES_BURST - 1):
        limiter.check(1, write=True)
    assert limiter.check(1, write=True).bucket == "writes"
    other = RateLimiter(clock=clock)
    for _ in range(framework.CALLS_BURST):
        other.check(1, write=False)
    assert other.check(1, write=True).bucket == "calls"
