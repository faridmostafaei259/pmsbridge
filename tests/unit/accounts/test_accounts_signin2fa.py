import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from conftest import (
    BASE_URL,
    EMAIL,
    PASSWORD,
    RECOVERY_CODE,
    MakeClient,
    add_authenticator,
    current_code,
    page_token,
    post_form,
    secret_from_page,
    sign_in,
)
from pmsbridge.accounts import twofactor
from pmsbridge.accounts.auth import LOGIN_COOKIE, SESSION_COOKIE
from pmsbridge.accounts.db import AuthFlow, RecoveryCode, Session, User, UserTotp
from pmsbridge.accounts.flows import FLOW_COOKIE
from pmsbridge.accounts.service import create_user, find_user_by_email
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import session, write_transaction

BAD_CODE = "That code is not correct."


async def rows[T](model: type[T]) -> list[T]:
    async with session() as db:
        return list((await db.execute(select(model))).scalars().all())


def cookie_names(response: httpx.Response) -> list[str]:
    return [value.split("=", 1)[0] for value in response.headers.get_list("set-cookie")]


async def verify(client: httpx.AsyncClient, code: str) -> httpx.Response:
    return await post_form(client, "/login/verify", {"code": code})


async def two_factor_user(email: str = EMAIL, role: str = "staff") -> tuple[int, bytes]:
    user = await create_user(email, PASSWORD, role=role)
    return user, await add_authenticator(user)


# ---- Two-step sign-in ----


async def test_a_correct_password_alone_gives_no_session(
    app: object, client: httpx.AsyncClient
) -> None:
    await two_factor_user()
    response = await sign_in(client, EMAIL, PASSWORD, next_path="/account/security")
    assert (response.status_code, response.headers["location"]) == (303, "/login/verify")
    assert SESSION_COOKIE not in cookie_names(response)
    assert FLOW_COOKIE in cookie_names(response)
    assert LOGIN_COOKIE in "".join(response.headers.get_list("set-cookie"))  # cleared
    assert await rows(Session) == []
    assert (await client.get("/")).status_code == 303
    page = await client.get("/login/verify")
    assert page.status_code == 200
    assert EMAIL in page.text
    assert "script-src" not in page.headers["content-security-policy"]


async def test_the_right_code_issues_a_session_and_goes_where_the_visitor_was_headed(
    app: object, client: httpx.AsyncClient
) -> None:
    _, secret = await two_factor_user()
    await sign_in(client, EMAIL, PASSWORD, next_path="/account/security")
    wrong = await verify(client, "000000")
    assert wrong.status_code == 400
    assert BAD_CODE in wrong.text
    assert SESSION_COOKIE not in cookie_names(wrong)
    done = await verify(client, current_code(secret))
    assert (done.status_code, done.headers["location"]) == (303, "/account/security")
    assert SESSION_COOKIE in cookie_names(done)
    assert (await client.get("/account/security")).status_code == 200
    assert await rows(AuthFlow) == []
    user = await find_user_by_email(EMAIL)
    assert user is not None
    assert user.last_login_at is not None


async def test_the_session_id_is_new_after_the_second_step_and_the_flow_cannot_be_replayed(
    app: object, client: httpx.AsyncClient, make_client: MakeClient
) -> None:
    _, secret = await two_factor_user()
    other = await create_user("other@example.test", PASSWORD)
    # The browser already holds a session (and a planted value in the flow's place).
    await sign_in(client, "other@example.test", PASSWORD)
    old = client.cookies[SESSION_COOKIE]
    assert other
    await sign_in(client, EMAIL, PASSWORD)
    client.cookies.set(SESSION_COOKIE, old, domain="pms.example.test")
    flow_cookie = client.cookies[FLOW_COOKIE]
    done = await verify(client, current_code(secret))
    assert done.status_code == 303
    fresh = client.cookies[SESSION_COOKIE]
    assert fresh != old
    assert fresh != flow_cookie
    thief = make_client("203.0.113.77")
    thief.cookies.set(SESSION_COOKIE, old, domain="pms.example.test")
    assert (await thief.get("/")).status_code == 303  # the earlier session ended
    # The same flow cookie, replayed after success, leads nowhere.
    replay = make_client("203.0.113.78")
    replay.cookies.set(FLOW_COOKIE, flow_cookie, domain="pms.example.test")
    assert (await replay.get("/login/verify")).status_code == 403


async def test_a_session_cookie_chosen_by_someone_else_is_never_adopted(
    app: object, client: httpx.AsyncClient
) -> None:
    _, secret = await two_factor_user()
    planted = "A" * 43
    await sign_in(client, EMAIL, PASSWORD)
    client.cookies.set(SESSION_COOKIE, planted, domain="pms.example.test")
    await verify(client, current_code(secret))
    assert client.cookies[SESSION_COOKIE] != planted
    stored = await rows(Session)
    assert len(stored) == 1


async def test_a_flow_ends_after_five_minutes_and_belongs_to_one_purpose(
    app: object, client: httpx.AsyncClient
) -> None:
    await two_factor_user()
    await sign_in(client, EMAIL, PASSWORD)
    [flow] = await rows(AuthFlow)
    assert (flow.expires_at - flow.created_at).total_seconds() == 300
    assert flow.kind == "second_factor"
    for path in ("/two-factor/enroll", "/setup", "/reset"):
        assert (await client.get(path)).status_code in (400, 403)
        assert (await client.get(path)).status_code != 200
    async with write_transaction() as db:
        await db.execute(update(AuthFlow).values(expires_at=flow.created_at))
    assert (await client.get("/login/verify")).status_code == 403


async def test_a_disabled_user_cannot_finish_the_second_step(
    app: object, client: httpx.AsyncClient
) -> None:
    user, secret = await two_factor_user()
    await sign_in(client, EMAIL, PASSWORD)
    token = page_token((await client.get("/login/verify")).text)
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == user).values(is_active=False))
    response = await client.post(
        "/login/verify",
        data={"code": current_code(secret), "csrf_token": token},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 403
    assert (await client.get("/login/verify")).status_code == 403
    assert SESSION_COOKIE not in cookie_names(response)


async def test_a_recovery_code_signs_in_once(app: object, client: httpx.AsyncClient) -> None:
    user, _ = await two_factor_user()
    async with write_transaction() as db:
        codes = await twofactor.replace_recovery_codes(db, user)
    await sign_in(client, EMAIL, PASSWORD)
    assert (await verify(client, codes[0].lower())).status_code == 303
    await sign_in(client, EMAIL, PASSWORD)
    assert (await verify(client, codes[0])).status_code == 400
    assert (await verify(client, codes[1])).status_code == 303


# ---- Wrong codes are limited ----


async def test_five_wrong_codes_lock_the_person_out_even_for_a_right_code(
    app: object, client: httpx.AsyncClient, make_client: MakeClient
) -> None:
    _, secret = await two_factor_user()
    await two_factor_user("bystander@example.test")
    await sign_in(client, EMAIL, PASSWORD)
    for _ in range(5):
        assert (await verify(client, "000000")).status_code == 400
    refused = await verify(client, current_code(secret))
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    assert SESSION_COOKIE not in cookie_names(refused)
    # Starting again with the password does not reset the count.
    await sign_in(client, EMAIL, PASSWORD)
    assert (await verify(client, current_code(secret))).status_code == 429
    # Someone else is not affected.
    other = make_client("203.0.113.40")
    await sign_in(other, "bystander@example.test", PASSWORD)
    assert (await verify(other, "000000")).status_code == 400


async def test_wrong_codes_sent_at_the_same_moment_do_not_all_get_checked(
    app: object, client: httpx.AsyncClient
) -> None:
    await two_factor_user()
    await sign_in(client, EMAIL, PASSWORD)
    html = (await client.get("/login/verify")).text
    token = html.split('name="csrf_token" value="')[1].split('"')[0]
    data = {"code": "000000", "csrf_token": token}
    responses = await asyncio.gather(
        *(client.post("/login/verify", data=data, headers={"Origin": BASE_URL}) for _ in range(12))
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses.count(400) == twofactor.CODE_MAX_FAILURES
    assert statuses.count(429) == 12 - twofactor.CODE_MAX_FAILURES


# ---- Administrators must have two-factor ----


async def test_an_administrator_without_two_factor_is_sent_to_enrollment_not_in(
    app: object, client: httpx.AsyncClient
) -> None:
    await create_user(EMAIL, PASSWORD, role="admin")
    response = await sign_in(client, EMAIL, PASSWORD, next_path="/account/security")
    assert response.headers["location"] == "/two-factor/enroll"
    assert SESSION_COOKIE not in cookie_names(response)
    assert await rows(Session) == []
    assert (await client.get("/")).status_code == 303
    assert (await client.get("/account/security")).status_code == 303

    page = await client.get("/two-factor/enroll")
    secret = secret_from_page(page.text)
    wrong = await post_form(client, "/two-factor/enroll", {"code": "123456"})
    assert wrong.status_code == 400
    assert await rows(Session) == []
    done = await post_form(client, "/two-factor/enroll", {"code": current_code(secret)})
    assert done.status_code == 200
    assert len(RECOVERY_CODE.findall(done.text)) == 10
    assert 'href="/account/security"' in done.text
    assert SESSION_COOKIE in cookie_names(done)
    assert (await client.get("/")).status_code == 200
    assert len(await rows(UserTotp)) == 1
    assert await rows(AuthFlow) == []
    # Next time: the second step.
    again = await sign_in(client, EMAIL, PASSWORD)
    assert again.headers["location"] == "/login/verify"


async def test_administrators_cannot_turn_two_factor_off(
    app: object, client: httpx.AsyncClient
) -> None:
    _, secret = await two_factor_user(role="admin")
    await sign_in(client, EMAIL, PASSWORD)
    await verify(client, current_code(secret))
    page = await client.get("/account/security")
    assert "/account/security/disable" not in page.text
    response = await post_form(
        client,
        "/account/security/disable",
        {"password": PASSWORD, "code": current_code(secret, 1)},
        page="/account/security",
    )
    assert response.status_code == 403
    assert len(await rows(UserTotp)) == 1


async def test_without_a_working_encryption_key_enrollment_says_so_and_stays_closed(
    app: object, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_user(EMAIL, PASSWORD, role="admin")
    await sign_in(client, EMAIL, PASSWORD)
    monkeypatch.setenv("ENCRYPTION_KEYS", "")
    clear_settings_cache()
    response = await client.get("/two-factor/enroll")
    assert response.status_code == 503
    assert "encryption key" in response.text
    assert await rows(Session) == []


# ---- Staff choose their own two-factor ----


async def test_staff_can_turn_two_factor_on_and_off_with_their_password_and_a_code(
    app: object, client: httpx.AsyncClient
) -> None:
    await create_user(EMAIL, PASSWORD)
    assert (await sign_in(client, EMAIL, PASSWORD)).headers["location"] == "/"
    page = await client.get("/account/security")
    assert "Turn on two-factor" in page.text

    started = await post_form(client, "/account/security/enable", {}, page="/account/security")
    assert (started.status_code, started.headers["location"]) == (303, "/two-factor/enroll")
    secret = secret_from_page((await client.get("/two-factor/enroll")).text)
    assert (await post_form(client, "/two-factor/enroll", {"code": "000000"})).status_code == 400
    done = await post_form(client, "/two-factor/enroll", {"code": current_code(secret)})
    assert done.status_code == 200
    assert len(RECOVERY_CODE.findall(done.text)) == 10
    assert SESSION_COOKIE not in cookie_names(done)  # already signed in: the session stays
    assert (await client.get("/account/security")).status_code == 200
    assert "10 unused recovery codes" in (await client.get("/account/security")).text

    # Turning it off needs the password and a code.
    bad_password = await post_form(
        client,
        "/account/security/disable",
        {"password": "not the password", "code": current_code(secret, 1)},
        page="/account/security",
    )
    assert bad_password.status_code == 400
    bad_code = await post_form(
        client,
        "/account/security/disable",
        {"password": PASSWORD, "code": "000000"},
        page="/account/security",
    )
    assert bad_code.status_code == 400
    assert len(await rows(UserTotp)) == 1
    off = await post_form(
        client,
        "/account/security/disable",
        {"password": PASSWORD, "code": current_code(secret, 1)},
        page="/account/security",
    )
    assert off.status_code == 303
    assert await rows(UserTotp) == []
    assert await rows(RecoveryCode) == []
    assert (await sign_in(client, EMAIL, PASSWORD)).headers["location"] == "/"


async def test_new_recovery_codes_need_the_password_and_a_code_and_replace_the_old_ones(
    app: object, client: httpx.AsyncClient
) -> None:
    user, secret = await two_factor_user()
    async with write_transaction() as db:
        old = await twofactor.replace_recovery_codes(db, user)
    await sign_in(client, EMAIL, PASSWORD)
    await verify(client, current_code(secret))
    refused = await post_form(
        client,
        "/account/security/recovery-codes",
        {"password": PASSWORD, "code": "000000"},
        page="/account/security",
    )
    assert refused.status_code == 400
    assert not RECOVERY_CODE.findall(refused.text)
    made = await post_form(
        client,
        "/account/security/recovery-codes",
        {"password": PASSWORD, "code": current_code(secret, 1)},
        page="/account/security",
    )
    assert made.status_code == 200
    new = RECOVERY_CODE.findall(made.text)
    assert len(new) == 10
    assert not set(new) & set(old)
    assert not await twofactor.use_code(user, old[0])
    assert await twofactor.use_code(user, new[0])


async def test_account_actions_need_a_session_csrf_and_the_same_person(
    app: object, client: httpx.AsyncClient, make_client: MakeClient
) -> None:
    for path in ("/account/security/enable", "/account/security/disable"):
        response = await client.post(path, data={"x": "y"}, headers={"Origin": BASE_URL})
        assert response.status_code == 401
    assert (await client.get("/account/security")).status_code == 303

    await create_user(EMAIL, PASSWORD)
    await create_user("bob@example.test", PASSWORD)
    await sign_in(client, EMAIL, PASSWORD)
    no_token = await client.post(
        "/account/security/enable", data={"x": "y"}, headers={"Origin": BASE_URL}
    )
    assert no_token.status_code == 403
    await post_form(client, "/account/security/enable", {}, page="/account/security")
    flow_cookie = client.cookies[FLOW_COOKIE]
    # Another signed-in person cannot use the first person's enrollment flow.
    bob = make_client("203.0.113.60")
    await sign_in(bob, "bob@example.test", PASSWORD)
    bob.cookies.set(FLOW_COOKIE, flow_cookie, domain="pms.example.test")
    assert (await bob.get("/two-factor/enroll")).status_code == 403
    # And someone with no session at all cannot either.
    stranger = make_client("203.0.113.61")
    stranger.cookies.set(FLOW_COOKIE, flow_cookie, domain="pms.example.test")
    assert (await stranger.get("/two-factor/enroll")).status_code == 403


# ---- Flow cookie attributes and the daily cap ----


def flow_cookie_attributes(response: httpx.Response) -> set[str]:
    cookie = next(v for v in response.headers.get_list("set-cookie") if v.startswith(FLOW_COOKIE))
    return {part.strip().lower() for part in cookie.split(";")}


async def test_sign_in_and_enrollment_flows_use_a_strict_cookie(
    app: object, client: httpx.AsyncClient, make_client: MakeClient
) -> None:
    await two_factor_user()
    second_step = await sign_in(client, EMAIL, PASSWORD)
    assert {"secure", "httponly", "samesite=strict", "path=/"} <= flow_cookie_attributes(
        second_step
    )
    assert not any(a.startswith("domain") for a in flow_cookie_attributes(second_step))

    await create_user("admin@example.test", PASSWORD, role="admin")
    enroll = await sign_in(make_client("203.0.113.50"), "admin@example.test", PASSWORD)
    assert "samesite=strict" in flow_cookie_attributes(enroll)

    await create_user("staff@example.test", PASSWORD)
    staff = make_client("203.0.113.51")
    await sign_in(staff, "staff@example.test", PASSWORD)
    enable = await post_form(staff, "/account/security/enable", {}, page="/account/security")
    assert "samesite=strict" in flow_cookie_attributes(enable)


async def test_twenty_wrong_codes_in_a_day_lock_the_person_out_for_the_day(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    user, secret = await two_factor_user()
    limiter = app.state.code_limiter  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    key = twofactor.limiter_key(user)
    for _ in range(twofactor.DAILY_MAX_FAILURES - 1):  # earlier failures, long since forgiven
        limiter.daily.record_failure(key)  # pyright: ignore[reportUnknownMemberType]
    await sign_in(client, EMAIL, PASSWORD)
    assert (await verify(client, "000000")).status_code == 400  # the twentieth
    refused = await verify(client, current_code(secret))
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > twofactor.CODE_WINDOW_SECONDS  # a day, not 15 min
    await sign_in(client, EMAIL, PASSWORD)
    assert (await verify(client, current_code(secret))).status_code == 429
