import argparse
import asyncio
import logging
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from conftest import BASE_URL, PASSWORD
from pmsbridge.accounts.db import OneTimeToken
from pmsbridge.accounts.service import create_user, find_user_by_email
from pmsbridge.admin import users as admin_users
from pmsbridge.core.settings import SettingsError
from pmsbridge.core.storage import session


@dataclass
class Result:
    code: int
    out: str
    err: str


def parser() -> argparse.ArgumentParser:
    built = argparse.ArgumentParser()
    admin_users.register(built.add_subparsers(dest="feature", required=True))
    return built


async def run(capsys: pytest.CaptureFixture[str], *arguments: str) -> Result:
    args = parser().parse_args(["users", *arguments])
    capsys.readouterr()
    # The handlers start their own event loop, so run them off this one.
    code: int = await asyncio.to_thread(args.handler, args)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


async def token_count() -> int:
    async with session() as db:
        return len((await db.execute(select(OneTimeToken))).scalars().all())


async def test_create_admin_prints_only_the_link(
    app: object, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    first = await run(capsys, "create-admin", "--email", "Root@Example.test")
    assert first.code == 0
    assert first.err == ""
    link = first.out.strip()
    assert first.out == f"{link}\n"
    assert link.startswith(f"{BASE_URL}/setup?token=")
    token = link.split("token=")[1]

    second = await run(capsys, "create-admin", "--email", "root@example.test")
    assert second.code == 0
    assert second.out.strip() != link  # a new link; the old one stops working
    assert await token_count() == 1

    logged = " ".join(
        f"{record.getMessage()} {record.__dict__}"
        for record in caplog.records
        if record.name.startswith("pmsbridge.admin")
    )
    assert "admin users create-admin" in logged
    assert token not in logged
    assert "example.test" not in logged


async def test_create_admin_refuses_an_existing_account_and_a_bad_address(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_user("staff@example.test", PASSWORD)
    taken = await run(capsys, "create-admin", "--email", "staff@example.test")
    assert taken.code == 1
    assert taken.out == ""
    assert "already exists" in taken.err
    bad = await run(capsys, "create-admin", "--email", "not-an-address")
    assert (bad.code, bad.out) == (2, "")
    assert "valid" in bad.err
    assert await token_count() == 0


async def test_reset_admin_password_prints_only_a_reset_link(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_user("root@example.test", PASSWORD, role="admin")
    done = await run(capsys, "reset-admin-password", "--email", "root@example.test")
    assert done.code == 0
    assert done.err == ""
    assert done.out.strip().startswith(f"{BASE_URL}/reset?token=")
    assert done.out.count("\n") == 1
    missing = await run(capsys, "reset-admin-password", "--email", "nobody@example.test")
    assert (missing.code, missing.out) == (1, "")
    assert "no administrator" in missing.err


def test_passwords_cannot_be_given_as_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    for action in ("create-admin", "reset-admin-password"):
        with pytest.raises(SystemExit):
            parser().parse_args(["users", action, "--email", "a@example.test", "--password", "x"])
    assert "unrecognized arguments" in capsys.readouterr().err


async def test_a_settings_error_exits_2_before_changing_anything(
    app: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise SettingsError("PUBLIC_BASE_URL is not set")

    monkeypatch.setattr(admin_users, "get_settings", broken)
    result = await run(capsys, "create-admin", "--email", "root@example.test")
    assert (result.code, result.out) == (2, "")
    assert result.err.startswith("Error: PUBLIC_BASE_URL is not set\nNext step:")
    assert await find_user_by_email("root@example.test") is None  # nothing was created
    again = await run(capsys, "reset-admin-password", "--email", "root@example.test")
    assert (again.code, again.out) == (2, "")


async def test_a_database_that_cannot_be_used_gives_a_clean_message_and_exit_2(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """No application has prepared the database, so the tables are missing."""
    caplog.set_level(logging.INFO)
    for action in ("create-admin", "reset-admin-password"):
        result = await run(capsys, action, "--email", "root@example.test")
        assert (result.code, result.out) == (2, "")
        assert result.err.startswith("Error: ")
        assert "Next step:" in result.err
        for leak in ("SELECT", "no such table", "Traceback", "root@example.test", "sqlite"):
            assert leak not in result.err
    assert "OperationalError" in caplog.text
    assert "SELECT" not in caplog.text
