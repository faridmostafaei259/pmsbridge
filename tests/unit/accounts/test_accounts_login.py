# ruff: noqa: S106  (the passwords in these tests are made-up values)
import asyncio
import json
import logging
import re
import time

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select, update

from conftest import BASE_URL, EMAIL, PASSWORD, MakeClient, page_token, sign_in
from pmsbridge.accounts import passwords
from pmsbridge.accounts.auth import SESSION_COOKIE
from pmsbridge.accounts.db import Session, User
from pmsbridge.accounts.pages import PAGE_CSP
from pmsbridge.accounts.ratelimit import ADDRESS_MAX_FAILURES, EMAIL_MAX_FAILURES
from pmsbridge.accounts.service import create_user
from pmsbridge.accounts.web import LoginForm
from pmsbridge.core.logging import configure_logging
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import session, write_transaction

GENERIC = "Email or password is incorrect."


def cookie_header(response: httpx.Response, name: str) -> str:
    matches = [value for value in response.headers.get_list("set-cookie") if value.startswith(name)]
    assert matches, f"no {name} cookie was set"
    return matches[0]


async def count_sessions() -> int:
    async with session() as db:
        return len((await db.execute(select(Session))).scalars().all())


# ---- Signing in and out ----


async def test_sign_in_page_has_a_form_and_strict_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert response.headers["content-security-policy"] == PAGE_CSP
    assert "script-src" not in PAGE_CSP
    assert "<script" not in response.text
    assert " style=" not in response.text
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert "content-encoding" not in response.headers


async def test_stylesheet_is_served_from_our_own_address(client: httpx.AsyncClient) -> None:
    response = await client.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


async def test_home_requires_sign_in_and_remembers_where_you_were_going(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


async def test_successful_sign_in_sets_a_hardened_cookie_and_opens_home(
    client: httpx.AsyncClient, user_id: int
) -> None:
    response = await sign_in(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = cookie_header(response, SESSION_COOKIE).lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "domain" not in cookie
    assert "max-age" not in cookie
    home = await client.get("/")
    assert home.status_code == 200
    assert EMAIL in home.text
    async with session() as db:
        user = (await db.execute(select(User))).scalar_one()
    assert user.last_login_at is not None


async def test_the_cookie_value_is_not_what_the_database_stores(
    client: httpx.AsyncClient, user_id: int
) -> None:
    response = await sign_in(client)
    value = cookie_header(response, SESSION_COOKIE).split(";")[0].split("=", 1)[1]
    async with session() as db:
        stored = (await db.execute(select(Session))).scalar_one()
    assert value != stored.id
    assert value not in stored.csrf_secret


async def test_every_sign_in_gets_a_new_session_and_ends_the_old_one(
    make_client: MakeClient, user_id: int
) -> None:
    first = await sign_in(make_client("203.0.113.5"))
    old = cookie_header(first, SESSION_COOKIE).split(";")[0].split("=", 1)[1]
    # A browser that still holds that cookie (for example in another tab) signs in again.
    other = make_client("203.0.113.6")
    token = page_token((await other.get("/login")).text)
    other.cookies.set(SESSION_COOKIE, old, domain="pms.example.test")
    second = await other.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": token},
        headers={"Origin": BASE_URL},
    )
    new = cookie_header(second, SESSION_COOKIE).split(";")[0].split("=", 1)[1]
    assert new != old
    assert await count_sessions() == 1
    other.cookies.set(SESSION_COOKIE, old, domain="pms.example.test")
    assert (await other.get("/")).status_code == 303


async def test_sign_out_ends_the_session_and_clears_the_cookie(
    client: httpx.AsyncClient, user_id: int
) -> None:
    await sign_in(client)
    token = page_token((await client.get("/")).text)
    response = await client.post(
        "/logout", data={"csrf_token": token}, headers={"Origin": BASE_URL}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "max-age=0" in cookie_header(response, SESSION_COOKIE).lower()
    assert await count_sessions() == 0
    assert (await client.get("/")).status_code == 303


async def test_signed_in_visitors_are_sent_away_from_the_sign_in_page(
    client: httpx.AsyncClient, user_id: int
) -> None:
    await sign_in(client)
    response = await client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("/dashboard?tab=1", "/dashboard?tab=1"),
        ("//evil.example", "/"),
        ("https://evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("/ok\r\nSet-Cookie: x=1", "/"),
        ("", "/"),
    ],
)
async def test_the_return_address_cannot_point_to_another_site(
    client: httpx.AsyncClient, user_id: int, requested: str, expected: str
) -> None:
    response = await sign_in(client, next_path=requested)
    assert response.status_code == 303
    assert response.headers["location"] == expected


async def test_sign_in_rehashes_an_outdated_password_hash(
    client: httpx.AsyncClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session() as db:
        before = (await db.execute(select(User))).scalar_one().password_hash
    monkeypatch.setattr(
        passwords, "_hasher", passwords.PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    )
    assert (await sign_in(client)).status_code == 303
    async with session() as db:
        after = (await db.execute(select(User))).scalar_one().password_hash
    assert after != before
    assert not passwords.needs_rehash(after)


# ---- Failures are generic ----


async def test_wrong_password_and_unknown_email_look_the_same(
    client: httpx.AsyncClient, user_id: int
) -> None:
    wrong = await sign_in(client, password="not the right password")
    unknown = await sign_in(client, email="nobody@example.test")
    for response in (wrong, unknown):
        assert response.status_code == 400
        assert GENERIC in response.text
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")

    def shape(response: httpx.Response, email: str) -> str:
        return re.sub(r'(csrf_token" value=")[^"]+', r"\1T", response.text.replace(email, "X"))

    assert shape(wrong, EMAIL) == shape(unknown, "nobody@example.test")


async def test_a_disabled_user_gets_the_same_generic_error(
    client: httpx.AsyncClient, user_id: int
) -> None:
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == user_id).values(is_active=False))
    response = await sign_in(client)
    assert response.status_code == 400
    assert GENERIC in response.text


@pytest.mark.parametrize(
    ("email", "password"),
    [("", ""), ("not-an-email", PASSWORD), (EMAIL, "x" * 5000), ("a" * 400 + "@example.test", "p")],
)
async def test_odd_input_is_a_generic_failure_not_an_error(
    client: httpx.AsyncClient, user_id: int, email: str, password: str
) -> None:
    response = await sign_in(client, email=email, password=password)
    assert response.status_code == 400
    assert GENERIC in response.text
    assert "x" * 100 not in response.text


async def test_unknown_email_still_pays_for_a_password_check(
    client: httpx.AsyncClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks: list[str] = []
    original = passwords.verify_password

    async def counting(password_hash: str, password: str) -> bool:
        checks.append(password_hash)
        return await original(password_hash, password)

    monkeypatch.setattr(passwords, "verify_password", counting)
    await sign_in(client, email="nobody@example.test")
    assert len(checks) == 1
    assert checks[0] == passwords._dummy_hash  # pyright: ignore[reportPrivateUsage]


# ---- CSRF ----


async def test_sign_in_without_a_token_is_refused(client: httpx.AsyncClient, user_id: int) -> None:
    await client.get("/login")
    response = await client.post(
        "/login", data={"email": EMAIL, "password": PASSWORD}, headers={"Origin": BASE_URL}
    )
    assert response.status_code == 403
    assert await count_sessions() == 0


async def test_sign_in_with_a_wrong_token_is_refused(
    client: httpx.AsyncClient, user_id: int
) -> None:
    await client.get("/login")
    response = await client.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": "A" * 86},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 403


async def test_login_csrf_a_token_from_another_browser_is_refused(
    make_client: MakeClient, user_id: int
) -> None:
    attacker, victim = make_client("198.51.100.1"), make_client("203.0.113.9")
    attacker_token = page_token((await attacker.get("/login")).text)
    await victim.get("/login")
    response = await victim.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": attacker_token},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 403


async def test_sign_in_without_the_login_cookie_is_refused(
    client: httpx.AsyncClient, user_id: int
) -> None:
    token = page_token((await client.get("/login")).text)
    client.cookies.clear()
    response = await client.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": token},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 403


async def test_a_cross_site_origin_is_refused_even_with_a_valid_token(
    client: httpx.AsyncClient, user_id: int
) -> None:
    token = page_token((await client.get("/login")).text)
    data = {"email": EMAIL, "password": PASSWORD, "csrf_token": token}
    for origin in ("https://evil.example", "null", "http://pms.example.test"):
        response = await client.post("/login", data=data, headers={"Origin": origin})
        assert response.status_code == 403, origin


async def test_without_origin_the_referer_decides(client: httpx.AsyncClient, user_id: int) -> None:
    token = page_token((await client.get("/login")).text)
    data = {"email": EMAIL, "password": PASSWORD, "csrf_token": token}
    bad = await client.post("/login", data=data, headers={"Referer": "https://evil.example/login"})
    assert bad.status_code == 403
    none = await client.post("/login", data=data)
    assert none.status_code == 403
    good = await client.post("/login", data=data, headers={"Referer": f"{BASE_URL}/login"})
    assert good.status_code == 303


async def test_sign_out_needs_a_token_and_the_right_origin(
    client: httpx.AsyncClient, user_id: int
) -> None:
    await sign_in(client)
    token = page_token((await client.get("/")).text)
    missing = await client.post("/logout", data={"x": "y"}, headers={"Origin": BASE_URL})
    wrong = await client.post(
        "/logout", data={"csrf_token": "A" * 86}, headers={"Origin": BASE_URL}
    )
    cross = await client.post(
        "/logout", data={"csrf_token": token}, headers={"Origin": "https://evil.example"}
    )
    assert (missing.status_code, wrong.status_code, cross.status_code) == (403, 403, 403)
    assert await count_sessions() == 1


async def test_the_token_in_a_page_differs_on_every_load(
    client: httpx.AsyncClient, user_id: int
) -> None:
    await sign_in(client)
    first = page_token((await client.get("/")).text)
    second = page_token((await client.get("/")).text)
    assert first != second


async def test_only_url_encoded_small_forms_are_read(client: httpx.AsyncClient) -> None:
    await client.get("/login")
    as_json = await client.post("/login", json={"email": EMAIL}, headers={"Origin": BASE_URL})
    assert as_json.status_code == 400
    big = await client.post("/login", data={"email": "a" * 30000}, headers={"Origin": BASE_URL})
    assert big.status_code == 413


# ---- Rate limiting ----


async def test_failures_for_one_email_lock_it_from_any_address(
    make_client: MakeClient, user_id: int
) -> None:
    for number in range(EMAIL_MAX_FAILURES):
        browser = make_client(f"198.51.100.{number + 1}")
        assert (await sign_in(browser, password="wrong password")).status_code == 400
    elsewhere = make_client("192.0.2.77")
    locked = await sign_in(elsewhere)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert SESSION_COOKIE not in locked.headers.get("set-cookie", "")
    other = await create_user("grace@example.test", "another fake passphrase")
    assert other
    assert (
        await sign_in(elsewhere, email="grace@example.test", password="another fake passphrase")
    ).status_code == 303


async def test_failures_from_one_address_lock_that_address_for_every_email(
    make_client: MakeClient, user_id: int
) -> None:
    noisy = make_client("203.0.113.50")
    for number in range(ADDRESS_MAX_FAILURES):
        await sign_in(noisy, email=f"guess{number}@example.test", password="wrong password")
    assert (await sign_in(noisy)).status_code == 429
    assert (await sign_in(make_client("203.0.113.51"))).status_code == 303


async def test_a_lockout_ends_and_a_success_clears_the_email_counter(
    app: FastAPI, client: httpx.AsyncClient, user_id: int
) -> None:
    now = [1000.0]
    limiter = app.state.login_limiter
    limiter.email._clock = limiter.address._clock = lambda: now[0]  # pyright: ignore[reportPrivateUsage]
    for _ in range(EMAIL_MAX_FAILURES):
        await sign_in(client, password="wrong password")
    assert (await sign_in(client)).status_code == 429
    now[0] += 6 * 60
    assert (await sign_in(client)).status_code == 303
    # The counter was cleared by the success: another run of failures starts from zero.
    for _ in range(EMAIL_MAX_FAILURES - 1):
        await sign_in(client, password="wrong password")
    assert (await sign_in(client)).status_code == 303


# ---- Secrets stay out of logs and errors ----


async def test_no_credentials_in_the_logs(
    client: httpx.AsyncClient,
    user_id: int,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(get_settings())
    root = logging.getLogger()
    root.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG):
            await sign_in(client, password="a secret nobody should log")
            await sign_in(client, email="nobody@example.test", password="another hidden value")
            await sign_in(client)
    finally:
        root.removeHandler(caplog.handler)
    shown = caplog.text + capsys.readouterr().out
    for secret in (
        "a secret nobody should log",
        "another hidden value",
        PASSWORD,
        EMAIL,
        "nobody@",
    ):
        assert secret not in shown
    lines = [line for line in shown.splitlines() if line.startswith("{")]
    assert any(json.loads(line)["message"] == "sign-in failed" for line in lines)


async def test_validation_errors_do_not_repeat_the_password() -> None:
    with pytest.raises(ValidationError) as caught:
        LoginForm(email="a@example.test", password="p" * 5000)
    assert "ppppp" not in str(caught.value)
    assert "ppppp" not in repr(caught.value)


async def test_the_submitted_password_is_in_no_response_or_database_column(
    client: httpx.AsyncClient, user_id: int
) -> None:
    secret = "visible nowhere at all"  # noqa: S105
    failed = await sign_in(client, password=secret)
    assert secret not in failed.text
    ok = await sign_in(client, password=PASSWORD)
    assert PASSWORD not in ok.text
    assert PASSWORD not in str(ok.headers)
    async with session() as db:
        rows = [
            *(await db.execute(select(User))).scalars(),
            *(await db.execute(select(Session))).scalars(),
        ]
    for row in rows:
        for column in row.__table__.columns:
            assert PASSWORD not in str(getattr(row, column.name))
            assert secret not in str(getattr(row, column.name))


# ---- Page headers, return paths, logout ----


async def test_every_page_with_a_form_sends_a_same_origin_referrer_policy(
    client: httpx.AsyncClient, user_id: int
) -> None:
    # The edge default (no-referrer) would make browsers post `Origin: null`.
    login = await client.get("/login")
    error = await client.post("/login", data={"email": EMAIL}, headers={"Origin": BASE_URL})
    wrong = await sign_in(client, password="not the right password")
    await sign_in(client)
    home = await client.get("/")
    for response in (login, error, wrong, home):
        assert response.headers["referrer-policy"] == "same-origin"
        assert "<form" in response.text or response.status_code == 403
    assert error.status_code == 403
    assert error.headers["referrer-policy"] == "same-origin"


async def test_a_long_authorize_style_return_path_survives_sign_in(
    client: httpx.AsyncClient, user_id: int
) -> None:
    target = "/authorize?" + "&".join(
        [
            "response_type=code",
            "client_id=" + "c" * 40,
            "redirect_uri=https%3A%2F%2Fapp.example.test%2Fcallback%2F" + "p" * 300,
            "code_challenge=" + "a" * 43,
            "code_challenge_method=S256",
            "state=" + "s" * 900,
            "resource=https%3A%2F%2Fpms.example.test%2Fmcp",
        ]
    )
    assert 1200 < len(target) < 2048
    page = await client.get("/login", params={"next": target})
    assert target in page.text.replace("&amp;", "&")
    response = await sign_in(client, next_path=target)
    assert response.headers["location"] == target


async def test_an_oversized_return_path_is_replaced_by_the_home_page(
    client: httpx.AsyncClient, user_id: int
) -> None:
    response = await sign_in(client, next_path="/x?" + "a" * 3000)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_the_dummy_hash_is_made_when_the_app_starts(app: FastAPI) -> None:
    assert passwords._dummy_hash is not None  # pyright: ignore[reportPrivateUsage]


async def test_sign_out_without_a_session_clears_a_stale_cookie_only_for_this_site(
    client: httpx.AsyncClient,
) -> None:
    from_site = await client.post("/logout", data={"x": "y"}, headers={"Origin": BASE_URL})
    assert from_site.status_code == 303
    assert "max-age=0" in cookie_header(from_site, SESSION_COOKIE).lower()
    for headers in ({"Origin": "https://evil.example"}, {}):
        other = await client.post("/logout", data={"x": "y"}, headers=headers)
        assert other.status_code == 303
        assert other.headers["location"] == "/login"
        assert "set-cookie" not in other.headers


# ---- Concurrent guesses ----


class CheckCounter:
    """Counts password checks and holds each one for a moment so that attempts overlap."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        original = passwords.verify_password

        async def counting(password_hash: str, password: str) -> bool:
            self.count += 1
            await asyncio.sleep(0.05)
            return await original(password_hash, password)

        monkeypatch.setattr(passwords, "verify_password", counting)


async def post_together(
    clients: list[httpx.AsyncClient], emails: list[str], password: str
) -> list[int]:
    tokens = [page_token((await client.get("/login")).text) for client in clients]
    responses = await asyncio.gather(
        *(
            client.post(
                "/login",
                data={"email": email, "password": password, "csrf_token": token},
                headers={"Origin": BASE_URL},
            )
            for client, email, token in zip(clients, emails, tokens, strict=True)
        )
    )
    return [response.status_code for response in responses]


async def test_simultaneous_guesses_for_one_email_stop_at_the_limit(
    make_client: MakeClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = CheckCounter(monkeypatch)
    total = EMAIL_MAX_FAILURES + 7
    clients = [make_client(f"198.51.100.{number + 1}") for number in range(total)]
    statuses = await post_together(clients, [EMAIL] * total, "wrong password here")
    assert checks.count <= EMAIL_MAX_FAILURES
    assert statuses.count(400) == EMAIL_MAX_FAILURES
    assert statuses.count(429) == total - EMAIL_MAX_FAILURES
    # The right password is refused afterwards, too.
    assert (await sign_in(make_client("192.0.2.200"))).status_code == 429


async def test_simultaneous_guesses_from_one_address_stop_at_the_limit(
    make_client: MakeClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = CheckCounter(monkeypatch)
    total = ADDRESS_MAX_FAILURES + 8
    clients = [make_client("203.0.113.70") for _ in range(total)]
    emails = [f"guess{number}@example.test" for number in range(total)]
    statuses = await post_together(clients, emails, "wrong password here")
    assert checks.count <= ADDRESS_MAX_FAILURES
    assert statuses.count(400) == ADDRESS_MAX_FAILURES
    assert statuses.count(429) == total - ADDRESS_MAX_FAILURES


async def test_a_right_password_is_refused_if_the_key_locks_while_it_is_checked(
    app: FastAPI, client: httpx.AsyncClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = passwords.verify_password

    async def locking(password_hash: str, password: str) -> bool:
        result = await original(password_hash, password)
        for entry in app.state.login_limiter.email._entries.values():  # pyright: ignore[reportPrivateUsage]
            entry.locked_until = time.monotonic() + 300
        return result

    monkeypatch.setattr(passwords, "verify_password", locking)
    response = await sign_in(client)
    assert response.status_code == 429
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert await count_sessions() == 0


# ---- Private addresses ----


async def test_private_addresses_have_a_much_higher_address_limit_but_the_same_email_limit(
    make_client: MakeClient, user_id: int
) -> None:
    gateway = make_client("172.18.0.1")
    for number in range(ADDRESS_MAX_FAILURES + 5):
        response = await sign_in(
            gateway, email=f"guess{number}@example.test", password="wrong password here"
        )
        assert response.status_code == 400
    assert (await sign_in(gateway)).status_code == 303
    for _ in range(EMAIL_MAX_FAILURES):
        await sign_in(gateway, password="wrong password here")
    assert (await sign_in(gateway)).status_code == 429


# ---- Slots are given back when a check fails or is cancelled ----


async def test_an_error_or_a_cancellation_during_the_check_gives_the_slots_back(
    app: FastAPI, user_id: int, make_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = app.state.login_limiter
    client = make_client("203.0.113.5")
    page = await client.get("/login")
    form = {"email": EMAIL, "password": PASSWORD, "csrf_token": page_token(page.text)}
    real_verify = passwords.verify_password

    def pending() -> int:
        entries = [*limiter.email._entries.values(), *limiter.address._entries.values()]  # pyright: ignore[reportUnknownMemberType,reportPrivateUsage]
        assert len(entries) == 2  # one entry per key
        return sum(entry.pending for entry in entries)  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]

    async def broken(password_hash: str, password: str) -> bool:
        raise RuntimeError("the hash check failed")

    monkeypatch.setattr(passwords, "verify_password", broken)
    failed = await client.post("/login", data=form, headers={"Origin": BASE_URL})
    assert failed.status_code == 500  # the error is contained and shows no details
    assert "hash check" not in failed.text
    assert pending() == 0

    started = asyncio.Event()

    async def hangs(password_hash: str, password: str) -> bool:
        started.set()
        await asyncio.Event().wait()
        return False

    monkeypatch.setattr(passwords, "verify_password", hangs)
    attempt = asyncio.create_task(client.post("/login", data=form, headers={"Origin": BASE_URL}))
    await started.wait()
    assert pending() == 2  # reserved while the check runs
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert pending() == 0

    monkeypatch.setattr(passwords, "verify_password", real_verify)
    assert limiter.reserve(EMAIL, "203.0.113.5") == 0  # a new attempt can reserve
    limiter.release(EMAIL, "203.0.113.5")
    assert (await sign_in(client)).status_code == 303
