"""`python -m pmsbridge.admin connections <action>`: secrets, exit codes, audit lines, output."""

import argparse
import asyncio
import io
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
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
)

from pmsbridge.admin import connections as admin_connections
from pmsbridge.pms.db import open_credentials
from pmsbridge.pms.http import BASE_URLS, Provider

NEW_ID, NEW_SECRET = "0new-client-id-5678", "new-secret-value-not-real"
LISTINGS = f"{BASE_URLS[Provider.GUESTY]}/listings"


@dataclass
class Result:
    code: int
    out: str
    err: str


async def run(capsys: pytest.CaptureFixture[str], *arguments: str) -> Result:
    built = argparse.ArgumentParser()
    admin_connections.register(built.add_subparsers(dest="feature", required=True))
    args = built.parse_args(["connections", *arguments])
    capsys.readouterr()
    code: int = await asyncio.to_thread(args.handler, args)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


def stdin_secret(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


async def test_list_says_when_there_are_none(capsys: pytest.CaptureFixture[str]) -> None:
    result = await run(capsys, "list")
    assert (result.code, result.out.strip()) == (0, "There are no connections yet.")


async def test_list_shows_the_columns_and_never_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection_id = await make_connection("Beach houses", account_id=ACCOUNT_ID)
    await put_token(connection_id, "t", expires_in=timedelta(hours=5), remaining=4)
    result = await run(capsys, "list")
    assert result.code == 0
    for expected in (connection_id, "Beach houses", "guesty", "Working", "off", "4"):
        assert expected in result.out
    assert CLIENT_ID not in result.out and CLIENT_SECRET not in result.out


async def test_add_reads_the_secret_from_stdin_and_keeps_the_validation_token(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_auth: FakeGuestyAuth,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    stdin_secret(monkeypatch, CLIENT_SECRET + "\n")
    result = await run(capsys, "add", "--label", "Main", "--client-id", CLIENT_ID, "--from-stdin")
    assert result.code == 0 and "Connection id:" in result.out
    connection_id = result.out.split("Connection id:")[1].strip()
    assert fake_auth.requests == 1
    assert await read_token_row(connection_id) is not None
    assert open_credentials(await read_connection(connection_id))["client_secret"] == CLIENT_SECRET
    for text in (result.out, result.err, caplog.text):
        assert CLIENT_SECRET not in text
    audit = [r for r in caplog.records if getattr(r, "connection_id", None) == connection_id]
    assert audit and getattr(audit[0], "acting_user") == "cli"  # noqa: B009


async def test_add_asks_for_the_secret_with_a_hidden_prompt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_auth: FakeGuestyAuth
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    prompts: list[str] = []

    def hidden(prompt: str) -> str:
        prompts.append(prompt)
        return CLIENT_SECRET

    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("getpass.getpass", hidden)
    result = await run(capsys, "add", "--label", "Main", "--client-id", CLIENT_ID)
    assert result.code == 0 and prompts == ["Client secret (hidden): "]
    assert CLIENT_SECRET not in result.out + result.err


async def test_add_without_a_terminal_or_stdin_flag_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_auth: FakeGuestyAuth
) -> None:
    stdin_secret(monkeypatch, "")
    result = await run(capsys, "add", "--label", "Main", "--client-id", CLIENT_ID)
    assert result.code == 2 and "--from-stdin" in result.err
    assert fake_auth.requests == 0


async def test_add_with_an_empty_secret_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_auth: FakeGuestyAuth
) -> None:
    stdin_secret(monkeypatch, "\n")
    result = await run(capsys, "add", "--label", "M", "--client-id", CLIENT_ID, "--from-stdin")
    assert result.code == 2 and fake_auth.requests == 0


async def test_the_secret_is_not_an_argument() -> None:
    built = argparse.ArgumentParser()
    admin_connections.register(built.add_subparsers(dest="feature", required=True))
    with pytest.raises(SystemExit):
        built.parse_args(
            ["connections", "add", "--label", "M", "--client-id", "x", "--secret", "s"]
        )


async def test_add_refuses_a_known_key_before_any_token_request(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_auth: FakeGuestyAuth
) -> None:
    await make_connection("First", account_id=ACCOUNT_ID)
    stdin_secret(monkeypatch, CLIENT_SECRET)
    result = await run(capsys, "add", "--label", "Second", "--client-id", CLIENT_ID, "--from-stdin")
    assert result.code == 1 and "already connected" in result.err
    assert fake_auth.requests == 0


async def test_add_with_credentials_guesty_refuses_exits_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_auth: FakeGuestyAuth
) -> None:
    stdin_secret(monkeypatch, "wrong-secret-value")
    result = await run(capsys, "add", "--label", "M", "--client-id", CLIENT_ID, "--from-stdin")
    assert result.code == 1 and "did not accept" in result.err
    assert "wrong-secret-value" not in result.out + result.err


async def test_rotate_replaces_the_credentials_and_keeps_the_id(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_auth: FakeGuestyAuth
) -> None:
    fake_auth.add_account(NEW_ID, NEW_SECRET, ACCOUNT_ID)
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID, status="needs_attention")
    stdin_secret(monkeypatch, NEW_SECRET)
    result = await run(
        capsys, "rotate", "--connection", "Main", "--client-id", NEW_ID, "--from-stdin"
    )
    assert result.code == 0 and connection_id in result.out
    conn = await read_connection(connection_id)
    assert conn.status == "active"
    assert open_credentials(conn)["client_id"] == NEW_ID
    assert NEW_SECRET not in result.out + result.err


async def test_rotate_with_bad_credentials_keeps_the_old_ones(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_auth: FakeGuestyAuth
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    stdin_secret(monkeypatch, "nope-nope")
    result = await run(
        capsys, "rotate", "--connection", connection_id, "--client-id", CLIENT_ID, "--from-stdin"
    )
    assert result.code == 1
    assert open_credentials(await read_connection(connection_id))["client_secret"] == CLIENT_SECRET


async def test_test_never_requests_a_token(
    capsys: pytest.CaptureFixture[str], fake_auth: FakeGuestyAuth, mock_guesty: respx.MockRouter
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    result = await run(capsys, "test", "--connection", "Main")
    assert result.code == 0 and "will be fetched the next time" in result.out
    await put_token(connection_id, make_jwt(CLIENT_ID, ACCOUNT_ID), expires_in=timedelta(hours=5))
    mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(200, json={"results": []}))
    good = await run(capsys, "test", "--connection", connection_id)
    assert good.code == 0 and "works" in good.out
    mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(401))
    bad = await run(capsys, "test", "--connection", connection_id)
    assert bad.code == 1 and "did not accept" in bad.err
    assert fake_auth.requests == 0


async def test_disable_and_enable(capsys: pytest.CaptureFixture[str]) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    assert (await run(capsys, "disable", "--connection", "Main")).code == 0
    assert (await read_connection(connection_id)).status == "disabled"
    assert (await run(capsys, "enable", "--connection", connection_id)).code == 0
    assert (await read_connection(connection_id)).status == "active"


async def test_allowing_changes_needs_yes_and_prints_the_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    refused = await run(capsys, "allow-changes", "--connection", "Main")
    assert refused.code == 2 and "--yes" in refused.err
    assert (await read_connection(connection_id)).writes_enabled is False
    allowed = await run(capsys, "allow-changes", "--connection", "Main", "--yes")
    assert allowed.code == 0 and "You are responsible" in allowed.out
    assert (await read_connection(connection_id)).writes_enabled is True
    assert (await run(capsys, "stop-changes", "--connection", "Main")).code == 0
    assert (await read_connection(connection_id)).writes_enabled is False


async def test_remove_needs_yes_and_deletes(capsys: pytest.CaptureFixture[str]) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    assert (await run(capsys, "remove", "--connection", "Main")).code == 2
    assert (await read_connection(connection_id)).label == "Main"
    assert (await run(capsys, "remove", "--connection", "Main", "--yes")).code == 0
    assert "There are no connections yet." in (await run(capsys, "list")).out


async def test_an_unknown_connection_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    for action in ("test", "disable", "enable", "stop-changes"):
        result = await run(capsys, action, "--connection", "Nothing")
        assert result.code == 1 and "no connection" in result.err, action


async def test_enable_and_disable_refuse_a_connection_that_needs_attention(
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection_id = await make_connection("Broken", status="needs_attention")
    enabled = await run(capsys, "enable", "--connection", "Broken")
    assert enabled.code == 1 and "Replace the credentials first" in enabled.err
    disabled = await run(capsys, "disable", "--connection", "Broken")
    assert disabled.code == 1 and "needs attention" in disabled.err
    assert (await read_connection(connection_id)).status == "needs_attention"
