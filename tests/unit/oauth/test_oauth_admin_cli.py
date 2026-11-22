"""`python -m pmsbridge.admin oauth revoke-ai-access`: selectors, exit codes and output."""

import argparse
import asyncio
import logging
from dataclasses import dataclass

import httpx
import pytest
from oauth_helpers import EMAIL, connect, sign_in

from pmsbridge.accounts.service import create_user
from pmsbridge.admin import oauth as admin_oauth
from pmsbridge.oauth.tokens import validate_access_token

OTHER_EMAIL = "grace@example.test"
OTHER_PASSWORD = "another fake passphrase"  # noqa: S105


@dataclass
class Result:
    code: int
    out: str
    err: str


@pytest.fixture
async def api(make_browser: object) -> httpx.AsyncClient:
    return make_browser("198.51.100.7")  # type: ignore[operator, no-any-return]


async def run(capsys: pytest.CaptureFixture[str], *arguments: str) -> Result:
    built = argparse.ArgumentParser()
    admin_oauth.register(built.add_subparsers(dest="feature", required=True))
    args = built.parse_args(["oauth", "revoke-ai-access", *arguments])
    capsys.readouterr()
    code: int = await asyncio.to_thread(args.handler, args)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


@dataclass
class Setup:
    mine: str
    mine_client: str
    theirs: str
    theirs_client: str


@pytest.fixture
async def two_people(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, make_browser: object
) -> Setup:
    await create_user(OTHER_EMAIL, OTHER_PASSWORD)
    other = make_browser("203.0.113.9")  # type: ignore[operator]
    assert (await sign_in(other, OTHER_EMAIL, OTHER_PASSWORD)).status_code == 303
    mine_client, mine = await connect(browser, api)
    theirs_client, theirs = await connect(other, api)
    return Setup(mine["access_token"], mine_client, theirs["access_token"], theirs_client)


async def alive(setup: Setup) -> tuple[bool, bool]:
    return (
        await validate_access_token(setup.mine) is not None,
        await validate_access_token(setup.theirs) is not None,
    )


async def test_a_selector_is_required(
    two_people: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    result = await run(capsys)
    assert result.code == 2 and "Choose who" in result.err and result.out == ""
    assert await alive(two_people) == (True, True)


async def test_all_needs_yes_and_stands_alone(
    two_people: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (await run(capsys, "--all")).code == 2
    assert (await run(capsys, "--all", "--yes", "--user", EMAIL)).code == 2
    assert await alive(two_people) == (True, True)


async def test_user_revokes_only_that_persons_approvals(
    two_people: Setup,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    person: int,
) -> None:
    caplog.set_level(logging.INFO)
    result = await run(capsys, "--user", EMAIL.upper())
    assert result.code == 0
    assert result.out == "Approvals cancelled: 1\nPeople affected: 1\nAssistants affected: 1\n"
    assert await alive(two_people) == (False, True)
    assert EMAIL not in caplog.text and two_people.mine not in caplog.text
    (record,) = [
        item for item in caplog.records if item.getMessage() == "admin oauth revoke-ai-access"
    ]
    assert record.target_user_id == person  # type: ignore[attr-defined]
    assert record.client_id is None  # type: ignore[attr-defined]
    assert record.grants == 1  # type: ignore[attr-defined]


async def test_the_audit_line_names_the_client_when_one_is_given(
    two_people: Setup, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert (await run(capsys, "--client", two_people.theirs_client)).code == 0
    (record,) = [
        item for item in caplog.records if item.getMessage() == "admin oauth revoke-ai-access"
    ]
    assert record.client_id == two_people.theirs_client  # type: ignore[attr-defined]
    assert record.target_user_id is None  # type: ignore[attr-defined]
    assert EMAIL not in caplog.text


async def test_client_revokes_every_approval_for_that_assistant(
    two_people: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    result = await run(capsys, "--client", two_people.theirs_client)
    assert result.code == 0 and "Approvals cancelled: 1" in result.out
    assert await alive(two_people) == (True, False)


async def test_user_and_client_together_narrow_the_choice(
    two_people: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    none = await run(capsys, "--user", EMAIL, "--client", two_people.theirs_client)
    assert none.code == 0 and "Approvals cancelled: 0" in none.out
    assert await alive(two_people) == (True, True)
    some = await run(capsys, "--user", EMAIL, "--client", two_people.mine_client)
    assert "Approvals cancelled: 1" in some.out
    assert await alive(two_people) == (False, True)


async def test_all_with_yes_revokes_everything(
    two_people: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    result = await run(capsys, "--all", "--yes")
    assert result.code == 0
    assert result.out == "Approvals cancelled: 2\nPeople affected: 2\nAssistants affected: 2\n"
    assert await alive(two_people) == (False, False)
    again = await run(capsys, "--all", "--yes")
    assert again.code == 0 and "Approvals cancelled: 0" in again.out


async def test_unknown_people_and_assistants_are_refused(
    two_people: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_user = await run(capsys, "--user", "nobody@example.test")
    assert missing_user.code == 1 and "no user" in missing_user.err
    unknown_client = await run(capsys, "--client", "u" * 24)
    assert unknown_client.code == 1 and "no assistant" in unknown_client.err
    malformed = await run(capsys, "--client", "../etc")
    assert malformed.code == 2
    assert await alive(two_people) == (True, True)
