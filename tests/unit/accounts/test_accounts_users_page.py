# ruff: noqa: S106  (the passwords and tokens in these tests are made-up values)
import asyncio
import logging
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from conftest import (
    BASE_URL,
    PASSWORD,
    MakeClient,
    add_authenticator,
    current_code,
    page_token,
    post_form,
    rewind_codes,
    sign_in,
    sign_in_with_code,
)
from pmsbridge.accounts import management
from pmsbridge.accounts.db import AuthFlow, OneTimeToken, RecoveryCode, Session, User, UserTotp
from pmsbridge.accounts.reauth import REAUTH_SECONDS, ReauthStore
from pmsbridge.accounts.service import create_user, find_user_by_email
from pmsbridge.accounts.twofactor import replace_recovery_codes
from pmsbridge.core.storage import session, write_transaction

ADMIN = "boss@example.test"
STAFF = "worker@example.test"
OTHER = "second.boss@example.test"
PAGE = "/admin/users"


async def rows[T](model: type[T]) -> list[T]:
    async with session() as db:
        return list((await db.execute(select(model))).scalars().all())


async def user_state(email: str) -> User:
    user = await find_user_by_email(email)
    assert user is not None
    return user


class Boss:
    """An administrator signed in with two-factor, who can confirm actions."""

    def __init__(self, client: httpx.AsyncClient, user: int, secret: bytes) -> None:
        self.client, self.user, self.secret = client, user, secret

    async def act(
        self, action: str, data: dict[str, str], *, confirm: bool = True, password: str = PASSWORD
    ) -> httpx.Response:
        """Post an action form, with the password and code of the confirmation step if asked."""
        if confirm:
            await rewind_codes(self.user)
            data = {
                **data,
                "reauth": "1",
                "password": password,
                "code": current_code(self.secret),
            }
        response = await post_form(self.client, f"{PAGE}/{action}", data, page=PAGE)
        if response.status_code == 303:  # success: show the page it leads to, as a browser would
            response = await self.client.get(response.headers["location"])
        return response


@pytest.fixture
async def boss(app: FastAPI, client: httpx.AsyncClient) -> Boss:
    user = await create_user(ADMIN, PASSWORD, role="admin")
    secret = await add_authenticator(user)
    await sign_in_with_code(client, user, secret, ADMIN)
    return Boss(client, user, secret)


async def staff_with_session(make_client: MakeClient, address: str = "203.0.113.50") -> int:
    user = await create_user(STAFF, PASSWORD)
    await sign_in(make_client(address), STAFF)
    return user


# ---- Access and the list ----


async def test_the_page_lists_people_with_status_two_factor_and_last_sign_in(
    boss: Boss, make_client: MakeClient
) -> None:
    await create_user(STAFF, PASSWORD)
    off = await create_user("off@example.test", PASSWORD)
    await management.disable_user(off, None)
    await management.invite_user("waiting@example.test", "admin", None)
    page = await boss.client.get(PAGE)
    assert page.status_code == 200
    for expected in (ADMIN, STAFF, "off@example.test", "waiting@example.test", "(you)"):
        assert expected in page.text
    assert "disabled" in page.text
    assert "Pending invitations" in page.text
    assert "never" in page.text
    assert "valid until" in page.text
    assert "script" not in page.headers["content-security-policy"]
    assert "no-store" in page.headers["cache-control"]
    # The administrator has no buttons for their own account.
    assert "Not available for your own account" in page.text


async def test_staff_cannot_open_or_use_the_page(app: object, client: httpx.AsyncClient) -> None:
    await create_user(STAFF, PASSWORD)
    await sign_in(client, STAFF)
    assert (await client.get(PAGE)).status_code == 403
    for action in ("invite", "disable", "enable", "reset-2fa", "role", "revoke"):
        response = await client.post(f"{PAGE}/{action}", data={}, headers={"Origin": BASE_URL})
        assert response.status_code == 403
    assert "Users" not in (await client.get("/")).text  # no link in the navigation


async def test_a_signed_out_visitor_is_sent_to_sign_in(
    app: object, client: httpx.AsyncClient
) -> None:
    response = await client.get(PAGE)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_administrators_see_the_users_link(boss: Boss) -> None:
    assert 'href="/admin/users"' in (await boss.client.get("/")).text


# ---- Recent confirmation ----


async def test_an_action_without_a_recent_confirmation_shows_the_confirmation_form_first(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    response = await boss.act("disable", {"user_id": str(target)}, confirm=False)
    assert response.status_code == 200
    assert "Confirm it is you" in response.text
    assert f'name="user_id" value="{target}"' in response.text
    assert 'name="password"' in response.text
    assert (await user_state(STAFF)).is_active
    assert len(await rows(Session)) == 2  # nothing ended


async def test_a_wrong_password_or_code_is_refused_and_changes_nothing(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    wrong_password = await boss.act("disable", {"user_id": str(target)}, password="not-it")
    assert wrong_password.status_code == 400
    assert "password or the code is not correct" in wrong_password.text
    wrong_code = await post_form(
        boss.client,
        f"{PAGE}/disable",
        {"user_id": str(target), "reauth": "1", "password": PASSWORD, "code": "000000"},
        page=PAGE,
    )
    assert wrong_code.status_code == 400
    assert (await user_state(STAFF)).is_active


async def test_confirmation_mistakes_are_limited_like_the_code_step(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    for _ in range(5):
        response = await post_form(
            boss.client,
            f"{PAGE}/disable",
            {"user_id": str(target), "reauth": "1", "password": PASSWORD, "code": "000000"},
            page=PAGE,
        )
        assert response.status_code == 400
    locked = await boss.act("disable", {"user_id": str(target)})  # a right password and code
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert "Try again in about 15 minutes" in locked.text
    assert "Wait 15 minutes" not in locked.text
    assert (await user_state(STAFF)).is_active


async def test_one_confirmation_covers_ten_minutes_then_it_is_asked_again(
    boss: Boss, app: FastAPI, make_client: MakeClient
) -> None:
    clock = {"now": 1000.0}
    app.state.reauth = ReauthStore(lambda: clock["now"])
    target = await staff_with_session(make_client)
    done = await boss.act("disable", {"user_id": str(target)})
    assert done.status_code == 200  # the users page it redirected to
    clock["now"] += REAUTH_SECONDS - 1
    again = await boss.act("enable", {"user_id": str(target)}, confirm=False)
    assert urlsplit(str(again.url)).path == PAGE  # no confirmation form
    assert (await user_state(STAFF)).is_active
    clock["now"] += 2
    asked = await boss.act("disable", {"user_id": str(target)}, confirm=False)
    assert "Confirm it is you" in asked.text
    assert (await user_state(STAFF)).is_active


async def test_the_confirmation_belongs_to_the_session(boss: Boss, make_client: MakeClient) -> None:
    target = await staff_with_session(make_client)
    await boss.act("role", {"user_id": str(target), "role": "admin"})
    other = await create_user(OTHER, PASSWORD, role="admin")
    secret = await add_authenticator(other, bytes(range(30, 50)))
    second_browser = make_client("203.0.113.60")
    await sign_in_with_code(second_browser, other, secret, OTHER)
    asked = await post_form(second_browser, f"{PAGE}/disable", {"user_id": str(target)}, page=PAGE)
    assert "Confirm it is you" in asked.text


async def test_the_forms_need_the_csrf_token_and_the_origin(boss: Boss) -> None:
    page = await boss.client.get(PAGE)
    data = {"user_id": "99", "reauth": "1", "password": PASSWORD, "code": "000000"}
    no_token = await boss.client.post(f"{PAGE}/disable", data=data, headers={"Origin": BASE_URL})
    assert no_token.status_code == 403
    bad_origin = await boss.client.post(
        f"{PAGE}/disable",
        data={**data, "csrf_token": page_token(page.text)},
        headers={"Origin": "https://evil.example.test"},
    )
    assert bad_origin.status_code == 403


# ---- Actions and safety rules ----


async def test_disabling_ends_sessions_and_flows_and_stops_sign_in(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    async with write_transaction() as db:
        await db.execute(
            AuthFlow.__table__.insert().values(
                id="f" * 64,
                user_id=target,
                kind="second_factor",
                csrf_secret="a" * 64,
                next_path="",
                created_at=(await user_state(STAFF)).created_at,
                expires_at=(await user_state(STAFF)).created_at,
            )
        )
    response = await boss.act("disable", {"user_id": str(target)})
    assert urlsplit(str(response.url)).path == PAGE
    assert "signed out everywhere" in response.text
    assert not (await user_state(STAFF)).is_active
    assert [row.user_id for row in await rows(Session)] == [boss.user]
    assert await rows(AuthFlow) == []
    blocked = await sign_in(make_client("203.0.113.51"), STAFF)
    assert blocked.status_code == 400


async def test_a_disabled_person_can_be_turned_on_again(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    await boss.act("disable", {"user_id": str(target)})
    page = await boss.client.get(PAGE)
    assert "Turn on" in page.text
    done = await boss.act("enable", {"user_id": str(target)}, confirm=False)
    assert "active again" in done.text
    assert (await user_state(STAFF)).is_active
    assert (await sign_in(make_client("203.0.113.52"), STAFF)).headers["location"] == "/"
    repeat = await boss.act("enable", {"user_id": str(target)}, confirm=False)
    assert repeat.status_code == 400
    assert "already active" in repeat.text


async def test_an_administrator_cannot_disable_demote_or_reset_themselves(boss: Boss) -> None:
    me = str(boss.user)
    for action, data in (
        ("disable", {"user_id": me}),
        ("role", {"user_id": me, "role": "staff"}),
        ("reset-2fa", {"user_id": me}),
    ):
        response = await boss.act(action, data)
        assert response.status_code == 400
        assert "your own account" in response.text
    me_now = await user_state(ADMIN)
    assert (me_now.is_active, me_now.role) == (True, "admin")
    assert len(await rows(UserTotp)) == 1


async def test_the_last_active_administrator_cannot_be_disabled_or_demoted(app: object) -> None:
    only = await create_user(ADMIN, PASSWORD, role="admin")
    for attempt in (
        management.disable_user(only, None),
        management.change_role(only, "staff", None),
    ):
        with pytest.raises(management.ActionRefusedError, match="last active administrator"):
            await attempt
    assert (await user_state(ADMIN)).is_active
    await create_user(OTHER, PASSWORD, role="admin")
    await management.change_role(only, "staff", None)  # allowed now: another administrator exists
    assert (await user_state(ADMIN)).role == "staff"


async def test_a_disabled_administrator_does_not_count_as_another_administrator(
    app: object,
) -> None:
    first = await create_user(ADMIN, PASSWORD, role="admin")
    second = await create_user(OTHER, PASSWORD, role="admin")
    await management.disable_user(second, first)
    with pytest.raises(management.ActionRefusedError, match="last active administrator"):
        await management.disable_user(first, None)


async def test_resetting_two_factor_removes_the_secret_and_codes_and_ends_sessions(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await create_user(STAFF, PASSWORD)
    secret = await add_authenticator(target, bytes(range(40, 60)))
    async with write_transaction() as db:
        await replace_recovery_codes(db, target)
    await sign_in_with_code(make_client("203.0.113.53"), target, secret, STAFF)
    assert len(await rows(RecoveryCode)) == 10
    done = await boss.act("reset-2fa", {"user_id": str(target)})
    assert "Two-factor sign-in was reset" in done.text
    assert "must set it up again" not in done.text
    assert [row.user_id for row in await rows(UserTotp)] == [boss.user]
    assert await rows(RecoveryCode) == []
    assert [row.user_id for row in await rows(Session)] == [boss.user]
    # Staff can sign in with the password alone now.
    assert (await sign_in(make_client("203.0.113.54"), STAFF)).headers["location"] == "/"


async def test_after_a_reset_an_administrator_must_enroll_again_at_next_sign_in(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await create_user(OTHER, PASSWORD, role="admin")
    secret = await add_authenticator(target, bytes(range(40, 60)))
    other_browser = make_client("203.0.113.55")
    await sign_in_with_code(other_browser, target, secret, OTHER)
    done = await boss.act("reset-2fa", {"user_id": str(target)})
    assert "must set it up again" in done.text
    assert (await other_browser.get("/")).status_code == 303  # session ended
    again = await sign_in(other_browser, OTHER)
    assert again.headers["location"] == "/two-factor/enroll"
    assert (await other_browser.get("/")).status_code == 303  # still no session


async def test_resetting_two_factor_for_a_person_without_it_is_refused(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    response = await boss.act("reset-2fa", {"user_id": str(target)})
    assert response.status_code == 400
    assert "not on" in response.text


async def test_promoting_staff_ends_their_sessions_and_the_two_factor_rule_applies(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    browser = make_client("203.0.113.56")
    await sign_in(browser, STAFF)
    done = await boss.act("role", {"user_id": str(target), "role": "admin"})
    assert "signed out everywhere" in done.text
    assert (await user_state(STAFF)).role == "admin"
    assert [row.user_id for row in await rows(Session)] == [boss.user]
    assert (await browser.get("/")).status_code == 303
    assert (await sign_in(browser, STAFF)).headers["location"] == "/two-factor/enroll"


async def test_demoting_an_administrator_works_when_another_remains(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await create_user(OTHER, PASSWORD, role="admin")
    done = await boss.act("role", {"user_id": str(target), "role": "staff"})
    assert "role was changed" in done.text
    assert (await user_state(OTHER)).role == "staff"
    same = await boss.act("role", {"user_id": str(target), "role": "staff"}, confirm=False)
    assert same.status_code == 400
    bad = await boss.act("role", {"user_id": str(target), "role": "owner"}, confirm=False)
    assert bad.status_code == 400


async def test_pending_people_cannot_be_disabled_or_changed_only_revoked(boss: Boss) -> None:
    await management.invite_user("waiting@example.test", "staff", None)
    pending = await user_state("waiting@example.test")
    for action, data in (
        ("disable", {"user_id": str(pending.id)}),
        ("enable", {"user_id": str(pending.id)}),
        ("reset-2fa", {"user_id": str(pending.id)}),
        ("role", {"user_id": str(pending.id), "role": "admin"}),
    ):
        assert (await boss.act(action, data)).status_code == 400
    revoked = await boss.act("revoke", {"user_id": str(pending.id)}, confirm=False)
    assert "invitation was cancelled" in revoked.text
    assert await find_user_by_email("waiting@example.test") is None


async def test_unknown_and_malformed_user_ids_fail_cleanly(boss: Boss) -> None:
    missing = await boss.act("disable", {"user_id": "99999"})
    assert missing.status_code == 400
    assert "no longer exists" in missing.text
    garbage = await boss.act("disable", {"user_id": "abc"}, confirm=False)
    assert garbage.status_code == 400
    assert "Traceback" not in garbage.text


# ---- Inviting from the page ----


async def test_inviting_shows_the_link_once_and_the_link_works(
    boss: Boss, make_client: MakeClient
) -> None:
    response = await boss.act("invite", {"email": " New@Example.test ", "role": "staff"})
    assert response.status_code == 200
    assert "Invitation ready" in response.text
    link = response.text.split(f"{BASE_URL}/invite?token=")[1].split("<")[0]
    assert len(link) == 43
    assert "no-store" in response.headers["cache-control"]
    page = await boss.client.get(PAGE)
    assert link not in page.text  # never shown again
    assert "new@example.test" in page.text
    assert "Pending invitations" in page.text
    invitee = make_client("203.0.113.57")
    opened = await invitee.get("/invite", params={"token": link})
    assert opened.headers["location"] == "/invite"


async def test_inviting_an_existing_address_or_a_bad_address_is_refused(boss: Boss) -> None:
    exists = await boss.act("invite", {"email": ADMIN.upper(), "role": "staff"})
    assert exists.status_code == 400
    assert "already exists" in exists.text
    bad = await boss.act("invite", {"email": "nonsense", "role": "staff"}, confirm=False)
    assert bad.status_code == 400
    assert "valid e-mail" in bad.text
    assert await rows(OneTimeToken) == []


async def test_inviting_again_from_the_page_replaces_the_link(boss: Boss) -> None:
    first = await boss.act("invite", {"email": "new@example.test", "role": "staff"})
    second = await boss.act("invite", {"email": "new@example.test", "role": "staff"}, confirm=False)
    assert first.text != second.text
    assert len(await rows(OneTimeToken)) == 1


# ---- Audit ----


async def test_every_action_logs_the_actor_target_and_action_without_addresses_or_tokens(
    boss: Boss, make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    target = await staff_with_session(make_client)
    invited = await boss.act("invite", {"email": "new@example.test", "role": "staff"})
    token = invited.text.split("token=")[1].split("<")[0]
    await boss.act("role", {"user_id": str(target), "role": "admin"}, confirm=False)
    await boss.act("disable", {"user_id": str(target)}, confirm=False)
    await boss.act("enable", {"user_id": str(target)}, confirm=False)
    lines = [
        record.__dict__ for record in caplog.records if record.getMessage() == "user administration"
    ]
    assert [(line["acting_user"], line["target_user_id"]) for line in lines][1:] == [
        (boss.user, target)
    ] * 3
    assert [line["action"] for line in lines] == [
        "invite as staff",
        "role admin",
        "disable",
        "enable",
    ]
    ours = "\n".join(
        f"{record.getMessage()} {record.__dict__}"
        for record in caplog.records
        if record.name.startswith("pmsbridge")
    )
    for leak in ("example.test", token, PASSWORD):
        assert leak not in ours


# ---- Hardening ----


@pytest.mark.parametrize("value", ["9" * 300, "0", "-1", "+5", " 7", "1e3", "01", "9" * 19])
async def test_a_malformed_user_id_is_a_plain_400(boss: Boss, value: str) -> None:
    for action in ("disable", "enable", "reset-2fa", "role"):
        response = await boss.act(action, {"user_id": value, "role": "staff"})
        assert response.status_code == 400
        assert "Traceback" not in response.text
    revoked = await boss.act("revoke", {"user_id": value}, confirm=False)
    assert revoked.status_code == 400


async def test_an_acting_administrator_who_lost_the_role_meanwhile_changes_nothing(
    app: object,
) -> None:
    first = await create_user(ADMIN, PASSWORD, role="admin")
    second = await create_user(OTHER, PASSWORD, role="admin")
    target = await create_user(STAFF, PASSWORD)
    await management.disable_user(first, second)
    for attempt in (
        management.invite_user("new@example.test", "staff", first),
        management.disable_user(target, first),
        management.enable_user(target, first),
        management.reset_two_factor(target, first),
        management.change_role(target, "admin", first),
        management.revoke_invite(target, first),
    ):
        with pytest.raises(management.ActionRefusedError, match="no longer do this"):
            await attempt
    assert (await user_state(STAFF)).role == "staff"
    assert await find_user_by_email("new@example.test") is None
    # A demoted administrator is refused as well, and so is a pending one.
    await management.enable_user(first, None)
    await management.change_role(second, "staff", None)
    with pytest.raises(management.ActionRefusedError, match="no longer do this"):
        await management.disable_user(target, second)


async def test_two_administrators_demoting_each_other_at_once_leave_one(app: object) -> None:
    first = await create_user(ADMIN, PASSWORD, role="admin")
    second = await create_user(OTHER, PASSWORD, role="admin")
    results = await asyncio.gather(
        management.change_role(second, "staff", first),
        management.change_role(first, "staff", second),
        return_exceptions=True,
    )
    refusals = [r for r in results if isinstance(r, management.ActionRefusedError)]
    assert len(refusals) == 1
    assert [r for r in results if r is None] == [None]
    async with session() as db:
        admins = (await db.execute(select(User).where(User.role == "admin"))).scalars().all()
    assert len(admins) == 1
    assert admins[0].is_active


async def test_an_invitation_and_its_cancellation_at_the_same_moment_never_fail_badly(
    app: object,
) -> None:
    admin = await create_user(ADMIN, PASSWORD, role="admin")
    for round_number in range(8):
        email = f"race{round_number}@example.test"
        await management.invite_user(email, "staff", admin)
        pending = await user_state(email)
        results = await asyncio.gather(
            management.invite_user(email, "admin", admin),
            management.revoke_invite(pending.id, admin),
            return_exceptions=True,
        )
        for result in results:
            assert result is None or isinstance(result, (str, management.ActionRefusedError))
        # Whatever the order, there is a link only for a person who exists.
        for link in await rows(OneTimeToken):
            assert any(person.id == link.user_id for person in await rows(User))


async def test_the_confirmation_page_names_the_person_and_the_new_role(
    boss: Boss, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    page = await boss.act("role", {"user_id": str(target), "role": "admin"}, confirm=False)
    assert STAFF in page.text
    assert "new role: administrator" in page.text
    invite_page = await boss.act(
        "invite", {"email": "x@example.test", "role": "staff"}, confirm=False
    )
    assert "x@example.test" in invite_page.text
    assert "new role: staff" in invite_page.text
    unknown = await boss.act("disable", {"user_id": "424242"}, confirm=False)
    assert "Confirm it is you" in unknown.text


async def test_logging_out_forgets_the_confirmation(
    boss: Boss, app: FastAPI, make_client: MakeClient
) -> None:
    target = await staff_with_session(make_client)
    await boss.act("role", {"user_id": str(target), "role": "admin"})
    store: ReauthStore = app.state.reauth
    assert len(store) == 1
    page = await boss.client.get("/")
    await boss.client.post(
        "/logout", data={"csrf_token": page_token(page.text)}, headers={"Origin": BASE_URL}
    )
    assert len(store) == 0


async def test_a_role_sent_with_the_password_form_of_an_invitation_is_ignored(
    app: object, client: httpx.AsyncClient
) -> None:
    token = await management.invite_user("new@example.test", "staff", None)
    await client.get("/invite", params={"token": token})
    done = await post_form(
        client,
        "/invite",
        {"password": "a-fake-passphrase-x", "confirm": "a-fake-passphrase-x", "role": "admin"},
    )
    assert done.status_code == 200
    stored = await user_state("new@example.test")
    assert (stored.role, stored.is_active) == ("staff", True)


@pytest.mark.parametrize("notice", ["<script>alert(1)</script>", "%0d%0aSet-Cookie:x", "x" * 5000])
async def test_a_crafted_notice_renders_nothing(boss: Boss, notice: str) -> None:
    page = await boss.client.get(PAGE, params={"notice": notice})
    assert page.status_code == 200
    assert 'role="status"' not in page.text
    assert "alert(1)" not in page.text


async def test_the_list_stops_at_the_cap_and_says_so(
    boss: Boss, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(management, "MAX_LISTED", 3)
    for number in range(4):
        await create_user(f"p{number}@example.test", PASSWORD)
    page = await boss.client.get(PAGE)
    assert "Showing the first 3 people only." in page.text
    assert "p0@example.test" in page.text
    assert "p2@example.test" not in page.text
    listed = await management.list_users()
    assert listed.truncated
    assert len(listed.rows) == 3
    monkeypatch.setattr(management, "MAX_LISTED", 500)
    assert not (await management.list_users()).truncated
