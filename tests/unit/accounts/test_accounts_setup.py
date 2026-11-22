# ruff: noqa: S105, S106  (the passwords and tokens in these tests are made-up values)
import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from conftest import (
    BASE_URL,
    PASSWORD,
    RECOVERY_CODE,
    add_authenticator,
    current_code,
    page_token,
    post_form,
    secret_from_page,
    sign_in,
)
from pmsbridge.accounts import totp
from pmsbridge.accounts.db import (
    AuthFlow,
    OneTimeToken,
    RecoveryCode,
    Session,
    User,
    UserTotp,
)
from pmsbridge.accounts.flows import FLOW_COOKIE, TOKEN_LIFETIME, issue_token
from pmsbridge.accounts.service import create_user, find_user_by_email
from pmsbridge.accounts.setup import LINK_INVALID, PasswordForm
from pmsbridge.admin import users as admin_users
from pmsbridge.core.storage import session, write_transaction

ADMIN_EMAIL = "root@example.test"
NEW_PASSWORD = "a-new-fake-passphrase"


def token_of(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


async def all_rows[T](model: type[T]) -> list[T]:
    async with session() as db:
        return list((await db.execute(select(model))).scalars().all())


async def open_link(client: httpx.AsyncClient, link: str) -> httpx.Response:
    parts = urlsplit(link)
    return await client.get(parts.path, params=parse_qs(parts.query))


async def choose_password(client: httpx.AsyncClient, path: str = "/setup") -> httpx.Response:
    data = {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    return await post_form(client, path, data)


# ---- The link ----


async def test_the_link_holds_a_random_token_and_only_its_hash_is_stored(app: object) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    assert link.startswith(f"{BASE_URL}/setup?token=")
    token = token_of(link)
    assert len(token) == 43
    [row] = await all_rows(OneTimeToken)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(vars(row))
    assert row.expires_at - row.created_at == TOKEN_LIFETIME == timedelta(hours=24)
    user = await find_user_by_email(ADMIN_EMAIL)
    assert user is not None
    assert (user.role, user.is_active, user.setup_pending) == ("admin", False, True)


async def test_a_pending_administrator_cannot_sign_in(
    app: object, client: httpx.AsyncClient
) -> None:
    await admin_users.create_admin(ADMIN_EMAIL)
    response = await sign_in(client, ADMIN_EMAIL, "!" * 12)
    assert response.status_code == 400


async def test_opening_the_link_does_not_use_it_and_the_token_leaves_the_address(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    token = token_of(link)
    for _ in range(2):
        response = await open_link(client, link)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
        assert token not in response.headers["location"]
    assert len(await all_rows(OneTimeToken)) == 1
    cookie = next(
        value for value in response.headers.get_list("set-cookie") if value.startswith(FLOW_COOKIE)
    )
    attributes = {part.strip().lower() for part in cookie.split(";")}
    assert {"secure", "httponly", "samesite=lax", "path=/"} <= attributes
    assert "Domain" not in cookie
    page = await client.get("/setup")
    assert page.status_code == 200
    assert token not in page.text
    assert (await client.get("/setup")).status_code == 200  # reloading the page keeps working


@pytest.mark.parametrize("token", ["", "short", "a" * 43, "a" * 500, "../" * 20])
async def test_unknown_and_malformed_tokens_all_give_the_same_answer(
    app: object, client: httpx.AsyncClient, token: str
) -> None:
    await admin_users.create_admin(ADMIN_EMAIL)
    response = await client.get("/setup", params={"token": token})
    assert response.status_code == 403
    assert LINK_INVALID in response.text


async def test_a_setup_link_does_not_work_on_the_reset_page_and_the_other_way_round(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    assert (await client.get("/reset", params={"token": token_of(link)})).status_code == 403


async def test_an_expired_link_is_refused(app: object, client: httpx.AsyncClient) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    async with write_transaction() as db:
        await db.execute(
            update(OneTimeToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    response = await open_link(client, link)
    assert response.status_code == 403
    assert LINK_INVALID in response.text


async def test_a_link_that_runs_out_while_its_flow_is_open_cannot_finish(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    await open_link(client, link)
    assert (await client.get("/setup")).status_code == 200  # the flow is still within its time
    async with write_transaction() as db:
        await db.execute(
            update(OneTimeToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    # An administrator finishes at enrollment; staff finish at the password form. Both refuse.
    chosen = await choose_password(client)
    assert chosen.status_code == 303
    secret = secret_from_page((await client.get("/two-factor/enroll")).text)
    late = await post_form(client, "/two-factor/enroll", {"code": current_code(secret)})
    assert late.status_code == 403
    assert LINK_INVALID in late.text
    user = await find_user_by_email(ADMIN_EMAIL)
    assert user is not None
    assert (user.is_active, user.setup_pending, user.password_hash) == (False, True, "!")
    assert await all_rows(UserTotp) == []


async def test_a_new_link_ends_the_old_one_and_a_flow_started_with_it(
    app: object, client: httpx.AsyncClient
) -> None:
    first = await admin_users.create_admin(ADMIN_EMAIL)
    await open_link(client, first)  # a flow now exists for the first link
    second = await admin_users.create_admin(ADMIN_EMAIL)
    assert token_of(first) != token_of(second)
    assert len(await all_rows(OneTimeToken)) == 1
    assert await all_rows(AuthFlow) == []
    assert (await client.get("/setup")).status_code == 403
    assert (await open_link(client, first)).status_code == 403
    assert (await open_link(client, second)).status_code == 303


async def test_create_admin_refuses_an_address_that_already_has_an_account(
    app: object,
) -> None:
    await create_user("active@example.test", PASSWORD, role="admin")
    await create_user("staff@example.test", PASSWORD, role="staff")
    for email in ("active@example.test", "STAFF@example.test"):
        with pytest.raises(admin_users.ActionRefusedError):
            await admin_users.create_admin(email)
    user = await find_user_by_email("active@example.test")
    assert user is not None
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == user.id).values(is_active=False))
    with pytest.raises(admin_users.ActionRefusedError):
        await admin_users.create_admin("active@example.test")
    assert await all_rows(OneTimeToken) == []


# ---- The first administrator, end to end ----


async def test_password_mistakes_are_explained_and_leave_the_account_untouched(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    await open_link(client, link)
    short = await post_form(client, "/setup", {"password": "short", "confirm": "short"})
    assert short.status_code == 400
    assert "at least 12" in short.text
    differ = await post_form(
        client, "/setup", {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD + "x"}
    )
    assert differ.status_code == 400
    assert "not the same" in differ.text
    long = await post_form(client, "/setup", {"password": "p" * 300, "confirm": "p" * 300})
    assert long.status_code == 400
    assert "ppppp" not in long.text
    user = await find_user_by_email(ADMIN_EMAIL)
    assert user is not None
    assert (user.is_active, user.setup_pending, user.password_hash) == (False, True, "!")
    assert len(await all_rows(OneTimeToken)) == 1


async def test_first_administrator_setup_from_link_to_signed_in(
    app: object,
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    link = await admin_users.create_admin(ADMIN_EMAIL)
    await open_link(client, link)
    chosen = await choose_password(client)
    assert (chosen.status_code, chosen.headers["location"]) == (303, "/two-factor/enroll")
    user = await find_user_by_email(ADMIN_EMAIL)
    assert user is not None
    assert not user.is_active  # not usable until two-factor is confirmed
    assert user.setup_pending

    enroll = await client.get("/two-factor/enroll")
    assert enroll.status_code == 200
    assert "<svg" in enroll.text
    assert 'href="otpauth://totp/' in enroll.text  # a link for phones
    assert "&amp;period=30" in enroll.text  # and the address as text
    assert "<script" not in enroll.text
    assert " style=" not in enroll.text
    secret = secret_from_page(enroll.text)
    assert len(secret) == 20
    assert secret_from_page((await client.get("/two-factor/enroll")).text) == secret  # stable

    wrong = await post_form(client, "/two-factor/enroll", {"code": "000000"})
    assert wrong.status_code == 400

    done = await post_form(client, "/two-factor/enroll", {"code": current_code(secret)})
    assert done.status_code == 200
    codes = RECOVERY_CODE.findall(done.text)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert "no-store" in done.headers["cache-control"]
    assert FLOW_COOKIE in "".join(done.headers.get_list("set-cookie"))  # cleared

    user = await find_user_by_email(ADMIN_EMAIL)
    assert user is not None
    assert user.is_active
    assert not user.setup_pending
    assert await all_rows(OneTimeToken) == []
    assert await all_rows(AuthFlow) == []
    [stored] = await all_rows(UserTotp)
    assert secret not in stored.secret  # encrypted with the vault
    hashes = {row.code_hash for row in await all_rows(RecoveryCode)}
    assert hashes == {hashlib.sha256(code.replace("-", "").encode()).hexdigest() for code in codes}

    assert (await open_link(client, link)).status_code == 403  # the link is used up
    # The password chosen in the flow now signs in, with the second step.
    fresh = client
    first = await sign_in(fresh, ADMIN_EMAIL, NEW_PASSWORD)
    assert first.headers["location"] == "/login/verify"
    # The code that confirmed enrollment is spent; the next step's code works.
    verified = await post_form(fresh, "/login/verify", {"code": current_code(secret, 1)})
    assert verified.status_code == 303
    assert (await fresh.get("/")).status_code == 200

    # Only the application's own log lines count: the test client logs the URLs it visits.
    ours = "\n".join(
        f"{record.getMessage()} {record.__dict__}"
        for record in caplog.records
        if record.name.startswith("pmsbridge")
    )
    assert "user signed in" in ours
    assert "two-factor sign-in enabled" in ours
    assert "account set up" in ours
    for secret_value in (token_of(link), NEW_PASSWORD, totp.secret_text(secret), *codes):
        assert secret_value not in ours


async def test_an_administrator_flow_stops_without_a_password_or_a_cookie(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    assert (await client.get("/two-factor/enroll")).status_code == 403
    assert (await client.post("/setup", data={"password": NEW_PASSWORD})).status_code == 403
    await open_link(client, link)
    # A flow with no password chosen yet cannot reach enrollment.
    assert (await client.get("/two-factor/enroll")).status_code == 400


async def test_setup_forms_need_the_right_csrf_token_and_origin(
    app: object, client: httpx.AsyncClient
) -> None:
    link = await admin_users.create_admin(ADMIN_EMAIL)
    await open_link(client, link)
    page = await client.get("/setup")
    data = {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    bad_token = await client.post(
        "/setup", data={**data, "csrf_token": "x" * 40}, headers={"Origin": BASE_URL}
    )
    assert bad_token.status_code == 403
    bad_origin = await client.post(
        "/setup",
        data={**data, "csrf_token": page_token(page.text)},
        headers={"Origin": "https://evil.example.test"},
    )
    assert bad_origin.status_code == 403
    assert (await find_user_by_email(ADMIN_EMAIL)).setup_pending  # pyright: ignore[reportOptionalMemberAccess]


async def test_a_staff_account_is_active_after_choosing_a_password_and_the_link_works_once(
    app: object, make_client: object
) -> None:
    async with write_transaction() as db:
        user = User(
            email="new@example.test",
            password_hash="!",
            role="staff",
            is_active=False,
            setup_pending=True,
            created_at=datetime.now(UTC),
        )
        db.add(user)
        await db.flush()
        user_id = user.id
    token = await issue_token(user_id, "setup")
    one, two = make_client("203.0.113.8"), make_client("203.0.113.9")  # type: ignore[operator]
    link = f"{BASE_URL}/setup?token={token}"
    await open_link(one, link)
    await open_link(two, link)  # a second browser may also open it; only one can finish
    data = {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}

    async def finish(client: httpx.AsyncClient) -> int:
        return (await post_form(client, "/setup", data)).status_code

    results = sorted(await asyncio.gather(finish(one), finish(two)))
    assert results == [200, 403]
    assert await all_rows(OneTimeToken) == []
    stored = await find_user_by_email("new@example.test")
    assert stored is not None
    assert (stored.is_active, stored.setup_pending) == (True, False)
    plain = (await sign_in(one, "new@example.test", NEW_PASSWORD)).headers["location"]
    assert plain == "/"  # staff without two-factor get a session straight away


# ---- Password reset for an administrator ----


async def test_reset_link_sets_a_new_password_keeps_two_factor_and_ends_sessions(
    app: object, make_client: object, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    admin = await create_user(ADMIN_EMAIL, PASSWORD, role="admin")
    secret = await add_authenticator(admin)
    browser = make_client("203.0.113.20")  # type: ignore[operator]
    await sign_in(browser, ADMIN_EMAIL, PASSWORD)
    await post_form(browser, "/login/verify", {"code": current_code(secret)})
    assert (await browser.get("/")).status_code == 200
    assert len(await all_rows(Session)) == 1

    link = await admin_users.reset_admin_password(ADMIN_EMAIL)
    assert "/reset?token=" in link
    other = make_client("203.0.113.21")  # type: ignore[operator]
    await open_link(other, link)
    assert (await choose_password(other, "/reset")).status_code == 200

    assert await all_rows(Session) == []
    assert (await browser.get("/")).status_code == 303
    assert await all_rows(OneTimeToken) == []
    assert (await sign_in(other, ADMIN_EMAIL, PASSWORD)).status_code == 400
    again = await sign_in(other, ADMIN_EMAIL, NEW_PASSWORD)
    assert again.headers["location"] == "/login/verify"  # two-factor is still on
    assert len(await all_rows(UserTotp)) == 1
    done = [r for r in caplog.records if r.getMessage() == "password reset completed"]
    assert len(done) == 1
    assert done[0].__dict__["user_id"] == admin
    assert "example.test" not in str(done[0].__dict__)


async def test_reset_also_ends_a_half_finished_sign_in_with_the_old_password(
    app: object, make_client: object
) -> None:
    admin = await create_user(ADMIN_EMAIL, PASSWORD, role="admin")
    secret = await add_authenticator(admin)
    attacker = make_client("203.0.113.30")  # type: ignore[operator]
    await sign_in(attacker, ADMIN_EMAIL, PASSWORD)  # password right, code still needed
    assert (await attacker.get("/login/verify")).status_code == 200
    link = await admin_users.reset_admin_password(ADMIN_EMAIL)
    owner = make_client("203.0.113.31")  # type: ignore[operator]
    await open_link(owner, link)
    assert (await choose_password(owner, "/reset")).status_code == 200
    assert (await attacker.get("/login/verify")).status_code == 403
    assert await all_rows(AuthFlow) == []
    assert secret


async def test_reset_admin_password_refuses_other_cases(app: object) -> None:
    await create_user("staff@example.test", PASSWORD, role="staff")
    await admin_users.create_admin("pending@example.test")
    disabled = await create_user("off@example.test", PASSWORD, role="admin")
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == disabled).values(is_active=False))
    for email in (
        "nobody@example.test",
        "staff@example.test",
        "pending@example.test",
        "off@example.test",
    ):
        with pytest.raises(admin_users.ActionRefusedError):
            await admin_users.reset_admin_password(email)


async def test_validation_errors_never_echo_the_password() -> None:
    with pytest.raises(ValidationError) as caught:
        PasswordForm.model_validate({"password": "p" * 400, "confirm": "q"})
    assert "ppppp" not in str(caught.value)
