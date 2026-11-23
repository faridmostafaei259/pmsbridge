"""The tool framework: connection checks, write gating, deadline, errors, audit, annotations."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from framework_tools import PMS_ERRORS, SECRET_TEXT, Spy, add_tools, make_loader
from mcp_helpers import call_tool, mint_token, result_text, rpc, seed_connection, seed_user
from sqlalchemy import select, update

from pmsbridge.core import audit
from pmsbridge.core.audit import AuditLog
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.mcp import framework
from pmsbridge.mcp.context import ToolContext
from pmsbridge.pms import registry
from pmsbridge.pms.base import Provider, remaining_seconds
from pmsbridge.pms.db import PMSConnection


class World:
    def __init__(self, client: httpx.AsyncClient, spy: Spy) -> None:
        self.client = client
        self.spy = spy
        self.user = 0
        self.token = ""
        self.ids: dict[str, str] = {}

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        return await call_tool(self.client, self.token, name, arguments)  # type: ignore[return-value]

    async def rows(self) -> list[AuditLog]:
        async with session() as db:
            return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


@pytest.fixture
async def world(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[World]:
    spy = Spy()
    add_tools(app.state.mcp_server, spy)
    built = World(client, spy)
    built.user = await seed_user()
    built.ids = {
        "beach": await seed_connection("Beach houses", writes_enabled=True),
        "ro": await seed_connection("Read only", writes_enabled=False),
        "off": await seed_connection("Switched off", status="disabled"),
        "attn": await seed_connection("Broken", status="needs_attention", writes_enabled=True),
        "low": await seed_connection("Low budget", status="token_budget_low"),
        "hosta": await seed_connection("Hostaway one", provider="hostaway", writes_enabled=True),
        "hidden": await seed_connection("Not for this assistant"),
    }
    labels = {
        built.ids["beach"]: ("Beach houses", Provider.GUESTY),
        built.ids["ro"]: ("Read only", Provider.GUESTY),
        built.ids["low"]: ("Low budget", Provider.GUESTY),
        built.ids["hosta"]: ("Hostaway one", Provider.HOSTAWAY),
        built.ids["hidden"]: ("Not for this assistant", Provider.GUESTY),
    }
    monkeypatch.setattr(registry, "load_provider", make_loader(spy, labels))
    allowed = tuple(value for key, value in built.ids.items() if key != "hidden")
    built.token, _ = await mint_token(built.user, connections=allowed)
    yield built


def error_text(result: dict[str, Any]) -> str:
    assert result["isError"] is True
    return result_text(result)


async def test_a_read_tool_works_on_a_named_connection(world: World) -> None:
    result = await world.call("t_read", connection="Beach houses", search="villa")
    assert result["isError"] is False
    assert result["structuredContent"] == {"connection": "Beach houses", "provider": "guesty"}
    assert world.spy.calls == [{"tool": "t_read", "connection": "Beach houses", "search": "villa"}]
    assert world.spy.closed == 1


async def test_the_connection_argument_is_added_to_every_pms_tool(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in listed}
    schema = by_name["t_read"]["inputSchema"]
    assert set(schema["properties"]) == {"search", "connection"}
    assert "required" not in schema or "connection" not in schema["required"]
    assert "list_connections" in schema["properties"]["connection"]["description"]
    assert by_name["t_write"]["inputSchema"]["required"] == ["text"]
    assert "call" not in schema["properties"]


async def test_annotations_mark_reads_and_writes(world: World) -> None:
    listed = (await rpc(world.client, world.token, "tools/list")).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in listed}
    read = by_name["t_read"]["annotations"]
    assert read["readOnlyHint"] is True and read["destructiveHint"] is False
    write = by_name["t_write"]["annotations"]
    assert write["readOnlyHint"] is False and write["destructiveHint"] is True
    assert by_name["list_connections"]["annotations"]["readOnlyHint"] is True
    assert "administrator has allowed changes" in by_name["t_write"]["description"]
    assert "administrator" not in by_name["t_read"]["description"]


async def test_a_write_needs_changes_to_be_allowed_on_the_connection(world: World) -> None:
    refused = await world.call("t_write", text="hello", connection="Read only")
    assert "Changes are not allowed" in error_text(refused)
    assert "Connections page" in result_text(refused)
    assert world.spy.calls == []  # the tool body never ran, and no provider was built
    assert world.spy.closed == 0
    allowed = await world.call("t_write", text="hello", connection="Beach houses")
    assert allowed["isError"] is False and allowed["structuredContent"] == {"added": True}


async def test_a_switch_is_checked_each_time(world: World) -> None:
    assert (await world.call("t_write", text="x", connection="Beach houses"))["isError"] is False
    async with write_transaction() as db:
        await db.execute(
            update(PMSConnection)
            .where(PMSConnection.id == world.ids["beach"])
            .values(writes_enabled=False)
        )
    assert "Changes are not allowed" in error_text(
        await world.call("t_write", text="x", connection="Beach houses")
    )


@pytest.mark.parametrize(
    ("label", "word"), [("Switched off", "turned off"), ("Broken", "needs attention")]
)
async def test_an_unusable_connection_is_refused_without_building_a_provider(
    world: World, label: str, word: str
) -> None:
    result = await world.call("t_read", connection=label)
    text = error_text(result)
    assert word in text and "administrator must fix it" in text
    assert world.spy.calls == [] and world.spy.closed == 0
    write = await world.call("t_write", text="x", connection=label)
    assert word in error_text(write)


async def test_a_low_token_budget_does_not_stop_a_call(world: World) -> None:
    assert (await world.call("t_read", connection="Low budget"))["isError"] is False


async def test_a_provider_specific_tool_on_the_wrong_provider(world: World) -> None:
    refused = await world.call("guesty_t_only", connection="Hostaway one")
    text = error_text(refused)
    assert "only works with Guesty connections" in text and "Hostaway connection" in text
    assert (await world.call("guesty_t_only", connection="Beach houses"))["isError"] is False
    assert world.spy.closed == 1


async def test_a_missing_capability_is_a_clear_error(world: World) -> None:
    result = await world.call("t_calendar", connection="Beach houses")
    assert "does not support 'calendar.read'" in error_text(result)
    assert world.spy.closed == 1


@pytest.mark.parametrize("kind", [name for name in PMS_ERRORS if name != "long"])
async def test_pms_errors_become_tool_errors_with_their_safe_message(
    world: World, kind: str
) -> None:
    result = await world.call("t_fail", kind=kind, connection="Beach houses")
    text = error_text(result)
    expected = PMS_ERRORS[kind].args[0]
    assert expected in text
    rows = await world.rows()
    assert rows[-1].status == "error" and rows[-1].error_class == type(PMS_ERRORS[kind]).__name__


async def test_a_rate_limit_error_says_when_to_try_again(world: World) -> None:
    text = error_text(await world.call("t_fail", kind="rate", connection="Beach houses"))
    assert "Try again in about 7 seconds" in text


async def test_a_very_long_error_is_cut_short(world: World) -> None:
    text = error_text(await world.call("t_fail", kind="long", connection="Beach houses"))
    assert len(text) < 700


@pytest.mark.parametrize("kind", ["crash", "value"])
async def test_a_crash_shows_nothing_from_the_exception(
    world: World, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    result = await world.call("t_fail", kind=kind, connection="Beach houses")
    text = error_text(result)
    assert SECRET_TEXT not in text and "Traceback" not in text and "RuntimeError" not in text
    assert "Something went wrong" in text
    assert SECRET_TEXT not in json.dumps(result)
    rows = await world.rows()
    assert rows[-1].status == "error"
    assert SECRET_TEXT not in repr(vars(rows[-1]))


async def test_the_deadline_is_set_for_the_call(world: World) -> None:
    result = await world.call("t_deadline", connection="Beach houses")
    remaining = result["structuredContent"]["remaining"]
    assert 20 < remaining <= framework.DEADLINE_SECONDS
    assert remaining_seconds() is None  # never leaks out of the call


async def test_a_slow_read_is_stopped_and_audited_as_a_timeout(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(framework, "DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(framework, "DEADLINE_GRACE_SECONDS", 0.1)
    text = error_text(await world.call("t_slow", connection="Beach houses"))
    assert "did not answer in time" in text
    assert (await world.rows())[-1].status == "timeout"
    assert world.spy.closed == 1


async def test_a_slow_write_says_the_outcome_is_unknown(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(framework, "DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(framework, "DEADLINE_GRACE_SECONDS", 0.1)
    text = error_text(await world.call("t_write", text="x", slow=True, connection="Beach houses"))
    assert "outcome is unknown" in text and "before trying again" in text
    assert len(world.spy.calls) == 1  # never retried


async def test_a_huge_result_is_refused(world: World) -> None:
    text = error_text(
        await world.call("t_big", size=framework.MAX_RESULT_BYTES + 10, connection="Beach houses")
    )
    assert "too large" in text and "filters" in text
    assert (await world.rows())[-1].error_class == "ResultTooLarge"
    small = await world.call("t_big", size=1000, connection="Beach houses")
    assert small["isError"] is False


async def test_arguments_cannot_change_who_is_calling(world: World) -> None:
    """The person and the connections come from the token; extra arguments are not used."""
    await world.call(
        "t_read",
        connection="Beach houses",
        user_id=9999,
        client_id="someone-else",
        allowed_connection_ids=[world.ids["hidden"]],
    )
    row = (await world.rows())[-1]
    assert row.user_id == world.user and row.client_id == "test-assistant"
    refused = await world.call("t_read", connection="Not for this assistant", user_id=9999)
    assert "no connection with that name or id" in error_text(refused)


async def test_audit_entries_say_who_what_and_how_long_and_nothing_else(world: World) -> None:
    await world.call("t_read", connection="Beach houses", search="Jane Doe passport 123")
    await world.call("t_write", text="a private note about Jane Doe", connection="Read only")
    await world.call("t_read", connection="No such thing")
    rows = await world.rows()
    assert [row.tool for row in rows] == ["t_read", "t_write", "t_read"]
    assert [row.status for row in rows] == ["ok", "denied", "denied"]
    assert rows[0].user_id == world.user and rows[0].client_id == "test-assistant"
    assert rows[0].connection_id == world.ids["beach"] and rows[0].error_class is None
    assert rows[1].error_class == "writes_disabled" and rows[1].connection_id == world.ids["ro"]
    assert rows[2].error_class == "unknown_connection" and rows[2].connection_id is None
    assert all(row.duration_ms >= 0 for row in rows)
    stored = json.dumps(
        [{k: str(v) for k, v in vars(row).items() if not k.startswith("_")} for row in rows]
    )
    for private in ("Jane", "passport", "private note", world.token):
        assert private not in stored
    columns = {column.name for column in AuditLog.__table__.columns}
    assert columns == {
        "id", "time", "user_id", "client_id", "connection_id", "tool", "status",
        "duration_ms", "error_class",
    }  # fmt: skip


async def test_an_audit_write_failure_does_not_fail_the_call(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> object:
        raise OSError("disk full")

    monkeypatch.setattr(audit, "write_transaction", broken)
    result = await world.call("t_read", connection="Beach houses")
    assert result["isError"] is False


async def test_a_call_that_is_cancelled_is_still_audited(world: World) -> None:
    run = framework._Run(
        tool="t_slow", write=False, ctx=ToolContext(world.user, "test-assistant", 1, frozenset())
    )

    async def body() -> None:
        async with framework._guarded(run):
            await asyncio.sleep(30)

    task = asyncio.create_task(body())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(40):
        rows = await world.rows()
        if rows:
            break
        await asyncio.sleep(0.05)
    assert [(row.tool, row.status, row.error_class) for row in rows] == [
        ("t_slow", "error", "Cancelled")
    ]
