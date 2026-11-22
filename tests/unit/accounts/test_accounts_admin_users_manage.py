import logging

import httpx
import pytest
from sqlalchemy import select
from test_accounts_admin_users import parser, run

from conftest import BASE_URL, PASSWORD, add_authenticator
from pmsbridge.accounts import management
from pmsbridge.accounts.db import RecoveryCode, Session, UserTotp
from pmsbridge.accounts.service import create_user, find_user_by_email
from pmsbridge.accounts.sessions import create_session
from pmsbridge.accounts.twofactor import replace_recovery_codes
from pmsbridge.core.storage import session, write_transaction


async def count(model: type[Session] | type[UserTotp] | type[RecoveryCode]) -> int:
    async with session() as db:
        return len((await db.execute(select(model))).scalars().all())


async def test_list_shows_every_account_and_no_secrets(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = await run(capsys, "list")
    assert (empty.code, empty.out) == (0, "There are no users yet.\n")
    admin = await create_user("root@example.test", PASSWORD, role="admin")
    await add_authenticator(admin)
    gone = await create_user("gone@example.test", PASSWORD)
    await run(capsys, "disable", "--email", "gone@example.test")
    await run(capsys, "invite", "--email", "waiting@example.test", "--role", "staff")
    result = await run(capsys, "list")
    assert result.code == 0
    assert result.err == ""
    lines = result.out.splitlines()
    assert lines[0].split() == ["EMAIL", "ROLE", "STATUS", "TWO-FACTOR", "LAST", "SIGN-IN"]
    body = {line.split()[0]: line.split() for line in lines[1:]}
    assert body["root@example.test"][1:5] == ["admin", "active", "on", "never"]
    assert body["gone@example.test"][1:4] == ["staff", "disabled", "off"]
    assert body["waiting@example.test"][1:4] == ["staff", "pending", "off"]
    assert "token" not in result.out
    assert gone


async def test_invite_prints_only_a_link_and_logs_without_it(
    app: object, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    first = await run(capsys, "invite", "--email", "New@Example.test", "--role", "admin")
    assert first.code == 0
    assert first.err == ""
    link = first.out.strip()
    assert first.out == f"{link}\n"
    assert link.startswith(f"{BASE_URL}/invite?token=")
    again = await run(capsys, "invite", "--email", "new@example.test", "--role", "staff")
    assert again.code == 0
    assert again.out.strip() != link
    user = await find_user_by_email("new@example.test")
    assert user is not None
    assert (user.role, user.setup_pending) == ("staff", True)
    logged = "\n".join(
        f"{record.getMessage()} {record.__dict__}"
        for record in caplog.records
        if record.name.startswith("pmsbridge")
    )
    assert "'acting_user': 'cli'" in logged
    assert link.split("token=")[1] not in logged
    assert "example.test" not in logged


async def test_invite_refusals_and_bad_input(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_user("staff@example.test", PASSWORD)
    taken = await run(capsys, "invite", "--email", "STAFF@example.test", "--role", "staff")
    assert (taken.code, taken.out) == (1, "")
    assert "already exists" in taken.err
    bad = await run(capsys, "invite", "--email", "nope", "--role", "staff")
    assert (bad.code, bad.out) == (2, "")
    with pytest.raises(SystemExit) as exit_info:
        parser().parse_args(["users", "invite", "--email", "a@example.test", "--role", "owner"])
    assert exit_info.value.code == 2
    with pytest.raises(SystemExit):
        parser().parse_args(["users", "invite", "--email", "a@example.test"])


async def test_disable_and_enable_end_sessions_and_report_refusals(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_user("root@example.test", PASSWORD, role="admin")
    staff = await create_user("staff@example.test", PASSWORD)
    await create_session(staff)
    done = await run(capsys, "disable", "--email", "Staff@Example.test")
    assert (done.code, done.err) == (0, "")
    assert "disabled" in done.out
    assert await count(Session) == 0
    repeat = await run(capsys, "disable", "--email", "staff@example.test")
    assert (repeat.code, repeat.out) == (1, "")
    assert "already disabled" in repeat.err
    back = await run(capsys, "enable", "--email", "staff@example.test")
    assert (back.code, back.err) == (0, "")
    assert (await find_user_by_email("staff@example.test")).is_active  # pyright: ignore[reportOptionalMemberAccess]
    again = await run(capsys, "enable", "--email", "staff@example.test")
    assert again.code == 1
    missing = await run(capsys, "disable", "--email", "nobody@example.test")
    assert (missing.code, missing.out) == (1, "")
    assert "no user" in missing.err


async def test_the_last_active_administrator_cannot_be_disabled_from_the_command_line(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    await create_user("root@example.test", PASSWORD, role="admin")
    refused = await run(capsys, "disable", "--email", "root@example.test")
    assert (refused.code, refused.out) == (1, "")
    assert "last active administrator" in refused.err
    assert (await find_user_by_email("root@example.test")).is_active  # pyright: ignore[reportOptionalMemberAccess]


async def test_reset_2fa_recovers_a_locked_out_administrator_even_the_last_one(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = await create_user("root@example.test", PASSWORD, role="admin")
    await add_authenticator(admin)
    async with write_transaction() as db:
        await replace_recovery_codes(db, admin)
    await create_session(admin)
    done = await run(capsys, "reset-2fa", "--email", "root@example.test")
    assert (done.code, done.err) == (0, "")
    assert "set it up again" in done.out
    assert await count(UserTotp) == 0
    assert await count(RecoveryCode) == 0
    assert await count(Session) == 0
    nothing = await run(capsys, "reset-2fa", "--email", "root@example.test")
    assert (nothing.code, nothing.out) == (1, "")
    assert "not on" in nothing.err


async def test_reset_2fa_for_staff_says_nothing_about_enrolling(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    staff = await create_user("staff@example.test", PASSWORD)
    await add_authenticator(staff)
    done = await run(capsys, "reset-2fa", "--email", "staff@example.test")
    assert done.code == 0
    assert "set it up again" not in done.out


async def test_pending_people_are_refused_by_every_account_action(
    app: object, capsys: pytest.CaptureFixture[str]
) -> None:
    await run(capsys, "invite", "--email", "waiting@example.test", "--role", "staff")
    for action in ("disable", "enable", "reset-2fa"):
        result = await run(capsys, action, "--email", "waiting@example.test")
        assert (result.code, result.out) == (1, "")


async def test_a_database_that_cannot_be_used_gives_exit_2_for_the_new_actions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for action in ("disable", "enable", "reset-2fa"):
        result = await run(capsys, action, "--email", "root@example.test")
        assert (result.code, result.out) == (2, "")
        assert "Next step:" in result.err
        assert "SELECT" not in result.err
    listed = await run(capsys, "list")
    assert (listed.code, listed.out) == (2, "")
    invited = await run(capsys, "invite", "--email", "root@example.test", "--role", "staff")
    assert (invited.code, invited.out) == (2, "")


async def test_create_admin_for_a_pending_person_cancels_an_earlier_invitation_link(
    app: object, capsys: pytest.CaptureFixture[str], client: httpx.AsyncClient
) -> None:
    invited = await run(capsys, "invite", "--email", "root@example.test", "--role", "admin")
    invite_link = invited.out.strip()
    await client.get("/invite", params={"token": invite_link.split("token=")[1]})  # a flow exists
    setup = await run(capsys, "create-admin", "--email", "root@example.test")
    assert setup.code == 0
    assert setup.out.strip().startswith(f"{BASE_URL}/setup?token=")
    assert (
        await client.get("/invite", params={"token": invite_link.split("token=")[1]})
    ).status_code == 403
    assert (await client.get("/invite")).status_code == 403  # the open flow ended too
    assert (
        await client.get("/setup", params={"token": setup.out.strip().split("token=")[1]})
    ).status_code == 303


async def test_list_says_when_it_shows_only_the_first_people(
    app: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(management, "MAX_LISTED", 2)
    for number in range(3):
        await create_user(f"p{number}@example.test", PASSWORD)
    result = await run(capsys, "list")
    assert result.code == 0
    assert result.out.rstrip().endswith("Showing the first 2 users only.")
    assert "p2@example.test" not in result.out
