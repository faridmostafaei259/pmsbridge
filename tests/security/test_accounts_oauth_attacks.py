"""Attacks on dashboard accounts and the OAuth server, run in process (no Docker, no network).

These are the hands-on attacks against accounts and OAuth that are cheap to repeat: open redirects,
cross-site form posts, sign-in lockout, redirect address and PKCE tricks, code and refresh-token
replay, tokens that must die with their person, hostile assistant web addresses, and pages that
must stay closed to staff. Each test states what an attacker tries and what must happen.
"""

import base64
import hashlib
import html
import re
from collections.abc import AsyncIterator, Callable
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport

from pmsbridge.accounts import management, passwords
from pmsbridge.accounts.service import create_user
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import reset_database
from pmsbridge.oauth.metadata_fetch import (
    MetadataFetchError,
    is_public_address,
    validate_client_id_url,
)
from pmsbridge.oauth.tokens import validate_access_token

BASE = "https://pms.example.test"
EMAIL = "staff-one@example.test"
PASSWORD = "fake-passphrase-for-tests"  # noqa: S105
CALLBACK = "https://assistant.example.test/callback"
VERIFIER = "fake-verifier-0123456789-0123456789-0123456789-abcdef"
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

MakeBrowser = Callable[[], httpx.AsyncClient]


def challenge(verifier: str = VERIFIER) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def token_in(page: httpx.Response) -> str:
    match = CSRF_FIELD.search(page.text)
    assert match, "no CSRF field on the page"
    return match.group(1)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("sec2"))
    clear_settings_cache()
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(passwords, "_dummy_hash", None)


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    yield create_app(("pmsbridge.core.web", "pmsbridge.accounts.web", "pmsbridge.oauth.web"))
    await reset_database()


@pytest.fixture
async def make_browser(app: FastAPI) -> AsyncIterator[MakeBrowser]:
    opened: list[httpx.AsyncClient] = []

    def build() -> httpx.AsyncClient:
        transport = ASGITransport(app=app, client=("203.0.113.5", 40000))
        client = httpx.AsyncClient(transport=transport, base_url=BASE)
        opened.append(client)
        return client

    yield build
    for client in opened:
        await client.aclose()


@pytest.fixture
async def staff(app: FastAPI) -> int:
    return await create_user(EMAIL, PASSWORD)


async def sign_in(
    client: httpx.AsyncClient,
    email: str = EMAIL,
    password: str = PASSWORD,
    next_path: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    page = await client.get("/login")
    return await client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": token_in(page),
            "next": next_path,
        },
        headers={"Origin": BASE} if headers is None else headers,
    )


@pytest.fixture
async def browser(make_browser: MakeBrowser, staff: int) -> httpx.AsyncClient:
    client = make_browser()
    assert (await sign_in(client)).status_code == 303
    return client


# ---- open redirects through the sign-in return path ----

HOSTILE_NEXT = [
    "//evil.example",
    "///evil.example",
    "/\\evil.example",
    "https://evil.example",
    "javascript:alert(1)",
    "/\t/evil.example",
    "/ /evil.example",
    "/\r\nSet-Cookie: x=y",
    "//evil.example/%2e%2e",
    "/" + "a" * 3000,
]


@pytest.mark.parametrize("hostile", HOSTILE_NEXT)
async def test_sign_in_return_path_never_leaves_the_site(
    make_browser: MakeBrowser, staff: int, hostile: str
) -> None:
    """A hostile link carries `next=<hostile>`: after sign-in the person lands on the home page."""
    response = await sign_in(make_browser(), next_path=hostile)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize("hostile", HOSTILE_NEXT)
async def test_sign_in_page_return_path_for_signed_in_person_is_local(
    browser: httpx.AsyncClient, hostile: str
) -> None:
    response = await browser.get("/login", params={"next": hostile})
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_unauthenticated_redirect_keeps_the_target_local(
    make_browser: MakeBrowser,
) -> None:
    response = await make_browser().get("//evil.example")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


# ---- cross-site form posts, login CSRF ----


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://evil.example",
        "https://pms.example.test.evil.example",
        "https://pms.example.test@evil.example",
        "https://pms.example.test:8443",
        "http://pms.example.test",
    ],
)
async def test_sign_in_refuses_a_cross_site_post_even_with_a_valid_token(
    make_browser: MakeBrowser, staff: int, origin: str | None
) -> None:
    headers = {} if origin is None else {"Origin": origin}
    response = await sign_in(make_browser(), headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers or "session" not in response.headers.get(
        "set-cookie", ""
    )


async def test_sign_in_refuses_a_token_taken_from_another_browser(
    make_browser: MakeBrowser, staff: int
) -> None:
    """Login CSRF: the attacker's own form token does not work with the victim's cookie."""
    victim, attacker = make_browser(), make_browser()
    await victim.get("/login")
    attacker_page = await attacker.get("/login")
    response = await victim.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": token_in(attacker_page)},
        headers={"Origin": BASE},
    )
    assert response.status_code == 403


async def test_a_forced_sign_out_needs_the_token(browser: httpx.AsyncClient) -> None:
    for headers in ({"Origin": "https://evil.example"}, {"Origin": BASE}):
        response = await browser.post("/logout", data={}, headers={**FORM, **headers})
        assert response.status_code in (400, 403)
        assert (await browser.get("/")).status_code == 200


# ---- brute force and lockout ----


async def test_lockout_treats_known_and_unknown_addresses_alike(
    make_browser: MakeBrowser, staff: int
) -> None:
    """Five wrong passwords lock sign-in for the address, known or not, with the same words."""

    async def hammer(email: str) -> list[httpx.Response]:
        client = make_browser()
        return [
            await sign_in(client, email=email, password=f"wrong-{n}-xxxxxxxx") for n in range(7)
        ]

    known, unknown = await hammer(EMAIL), await hammer("nobody@example.test")
    assert [r.status_code for r in known] == [400] * 5 + [429] * 2
    assert [r.status_code for r in unknown] == [r.status_code for r in known]
    assert known[0].text.count("incorrect") == unknown[0].text.count("incorrect") == 1
    assert known[-1].headers["retry-after"] == unknown[-1].headers["retry-after"]
    # The right password does not get through while the lock is on.
    assert (await sign_in(make_browser())).status_code == 429


# ---- authorization endpoint tricks ----


async def register(api: httpx.AsyncClient, **overrides: object) -> str:
    body: dict[str, object] = {
        "client_name": "Example Assistant",
        "redirect_uris": [CALLBACK, "http://127.0.0.1/callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        **overrides,
    }
    response = await api.post("/oauth/register", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["client_id"])


def params(client_id: str, **overrides: str | None) -> dict[str, str]:
    base: dict[str, str | None] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge(),
        "code_challenge_method": "S256",
        "state": "state-123",
        **overrides,
    }
    return {name: value for name, value in base.items() if value is not None}


async def approve(
    client: httpx.AsyncClient, query: dict[str, str], decision: str = "approve"
) -> httpx.Response:
    page = await client.get("/oauth/authorize", params=query)
    assert page.status_code == 200, page.text
    hidden = {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input type="hidden" name="(?!csrf_token)([^"]+)" value="([^"]*)"', page.text
        )
    }
    return await client.post(
        "/oauth/authorize",
        data={**hidden, "csrf_token": token_in(page), "decision": decision},
        headers={"Origin": BASE},
    )


async def code_for(browser: httpx.AsyncClient, client_id: str, **overrides: str | None) -> str:
    response = await approve(browser, params(client_id, **overrides))
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


async def exchange(
    api: httpx.AsyncClient, client_id: str, code: str, **overrides: str | None
) -> httpx.Response:
    form: dict[str, str | None] = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": CALLBACK,
        "code_verifier": VERIFIER,
        **overrides,
    }
    return await api.post(
        "/oauth/token", data={k: v for k, v in form.items() if v is not None}, headers=FORM
    )


HOSTILE_REDIRECTS = [
    CALLBACK + "/",
    CALLBACK + "?x=1",
    CALLBACK + "#frag",
    "https://assistant.example.test.evil.example/callback",
    "https://ASSISTANT.example.test/callback",
    "https://assistant.example.test@evil.example/callback",
    "https://assistant.example.test:8443/callback",
    "https://assistant.example.test/callback/../x",
    "http://assistant.example.test/callback",
    "https://evil.example/callback",
    "//evil.example/callback",
    "http://127.0.0.1.evil.example/callback",
    "http://127.0.0.2:9/callback",
    "http://127.1/callback",
    "http://localhost:9/callback",
    "http://127.0.0.1:9/callback2",
    "http://127.0.0.1:9/callback?x=1",
    "",
]


@pytest.mark.parametrize("redirect", HOSTILE_REDIRECTS)
async def test_authorize_never_redirects_to_an_unregistered_address(
    browser: httpx.AsyncClient, make_browser: MakeBrowser, redirect: str
) -> None:
    client_id = await register(make_browser())
    response = await browser.get(
        "/oauth/authorize", params=params(client_id, redirect_uri=redirect)
    )
    assert response.status_code == 400
    assert "location" not in response.headers


async def test_registered_loopback_may_change_only_its_port(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    client_id = await register(make_browser())
    ok = await browser.get(
        "/oauth/authorize", params=params(client_id, redirect_uri="http://127.0.0.1:5555/callback")
    )
    assert ok.status_code == 200


@pytest.mark.parametrize(
    "override",
    [
        {"code_challenge": None},
        {"code_challenge": "short"},
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"response_type": "token"},
        {"response_type": "code token"},
        {"resource": "https://evil.example/mcp"},
        {"resource": BASE + "/mcp/../other"},
        {"resource": BASE + "/mcp?x=1"},
        {"state": "s" * 600},
    ],
)
async def test_authorize_request_errors_never_redirect_by_themselves(
    browser: httpx.AsyncClient, make_browser: MakeBrowser, override: dict[str, str | None]
) -> None:
    """A weak or odd request shows an error page; the browser is not sent to the client."""
    client_id = await register(make_browser())
    response = await browser.get("/oauth/authorize", params=params(client_id, **override))
    assert response.status_code == 400
    assert "location" not in response.headers


async def test_repeated_parameters_are_refused(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    client_id = await register(make_browser())
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params(client_id).items())
    for extra in (
        "redirect_uri=https%3A%2F%2Fevil.example%2Fcb",
        "state=2",
        f"client_id={client_id}",
    ):
        response = await browser.get(f"/oauth/authorize?{query}&{extra}")
        assert response.status_code == 400
        assert "location" not in response.headers


async def test_consent_page_cannot_be_framed_and_escapes_the_assistant_name(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    client_id = await register(make_browser(), client_name='<img src=x onerror=alert(1)> "q')
    page = await browser.get("/oauth/authorize", params=params(client_id))
    assert "<img src=x" not in page.text and "&lt;img" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    policy = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy and "script-src" not in policy
    assert "default-src 'none'" in policy
    assert "form-action 'self' https://assistant.example.test" in policy


async def test_consent_post_rechecks_everything_the_form_carries(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    client_id = await register(make_browser())
    page = await browser.get("/oauth/authorize", params=params(client_id))
    hidden = {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input type="hidden" name="(?!csrf_token)([^"]+)" value="([^"]*)"', page.text
        )
    }
    good = {**hidden, "csrf_token": token_in(page), "decision": "approve"}
    for tampered in (
        {**good, "redirect_uri": "https://evil.example/callback"},
        {**good, "resource": "https://evil.example/mcp"},
        {**good, "response_type": "token"},
        {**good, "decision": "approve_all"},
        {k: v for k, v in good.items() if k != "csrf_token"},
    ):
        response = await browser.post("/oauth/authorize", data=tampered, headers={"Origin": BASE})
        assert response.status_code in (400, 403)
        assert "location" not in response.headers
    response = await browser.post(
        "/oauth/authorize", data=good, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


# ---- codes, PKCE and refresh tokens ----


async def test_pkce_downgrade_and_theft_attempts_fail_and_leave_the_code_for_its_owner(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    api = make_browser()
    owner, other = await register(api), await register(api, redirect_uris=["https://o.example/cb"])
    attempts: list[dict[str, str | None]] = [
        {"code_verifier": None},
        {"code_verifier": challenge()},  # the challenge used as the verifier (plain)
        {"code_verifier": "short"},
        {"code_verifier": "x" * 43},
        {"redirect_uri": CALLBACK + "2"},
        {"redirect_uri": None},
        {"resource": "https://evil.example/mcp"},
    ]
    for override in attempts:
        code = await code_for(browser, owner)
        assert (await exchange(api, owner, code, **override)).status_code == 400
        assert (await exchange(api, owner, code)).status_code == 200  # still the owner's
    stolen = await code_for(browser, owner)
    assert (await exchange(api, other, stolen)).status_code == 400
    assert (await exchange(api, owner, stolen)).status_code == 200


async def test_code_replay_revokes_every_token_of_the_approval(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    api = make_browser()
    client_id = await register(api)
    code = await code_for(browser, client_id)
    first = (await exchange(api, client_id, code)).json()
    assert await validate_access_token(first["access_token"]) is not None
    assert (await exchange(api, client_id, code)).status_code == 400
    assert await validate_access_token(first["access_token"]) is None
    refreshed = await api.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": first["refresh_token"],
        },
        headers=FORM,
    )
    assert refreshed.status_code == 400


async def test_refresh_token_reuse_and_cross_client_use(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    api = make_browser()
    client_id = await register(api)
    other = await register(api, redirect_uris=["https://o.example/cb"])
    issued = (await exchange(api, client_id, await code_for(browser, client_id))).json()

    async def refresh(client: str, token: str, **extra: str) -> httpx.Response:
        return await api.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client,
                "refresh_token": token,
                **extra,
            },
            headers=FORM,
        )

    stolen_by_other = await refresh(other, issued["refresh_token"])
    assert stolen_by_other.status_code == 400
    assert (await refresh(client_id, issued["refresh_token"], scope="mcp admin")).status_code == 400
    rotated = (await refresh(client_id, issued["refresh_token"])).json()
    assert rotated["refresh_token"] != issued["refresh_token"]
    # The retired token comes back: the approval is revoked, newest tokens included.
    assert (await refresh(client_id, issued["refresh_token"])).status_code == 400
    assert await validate_access_token(rotated["access_token"]) is None
    assert (await refresh(client_id, rotated["refresh_token"])).status_code == 400


async def test_a_refresh_token_is_not_an_access_token(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    api = make_browser()
    client_id = await register(api)
    issued = (await exchange(api, client_id, await code_for(browser, client_id))).json()
    assert await validate_access_token(issued["refresh_token"]) is None
    assert await validate_access_token(issued["access_token"]) is not None


@pytest.mark.parametrize("change", ["disable", "make-admin"])
async def test_access_tokens_die_with_the_persons_standing(
    browser: httpx.AsyncClient, make_browser: MakeBrowser, staff: int, change: str
) -> None:
    api = make_browser()
    client_id = await register(api)
    issued = (await exchange(api, client_id, await code_for(browser, client_id))).json()
    assert await validate_access_token(issued["access_token"]) is not None
    if change == "disable":
        await management.disable_user(staff, None)
    else:
        await management.change_role(staff, "admin", None)
    assert await validate_access_token(issued["access_token"]) is None
    if change == "disable":
        await management.enable_user(staff, None)
        assert await validate_access_token(issued["access_token"]) is None  # stays revoked


async def test_secret_clients_need_the_right_secret_and_the_right_method(
    browser: httpx.AsyncClient, make_browser: MakeBrowser
) -> None:
    api = make_browser()
    created = await api.post(
        "/oauth/register",
        json={
            "client_name": "Confidential",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    client_id, secret = created.json()["client_id"], created.json()["client_secret"]
    for extra in ({}, {"client_secret": "x" * 43}):
        code = await code_for(browser, client_id)
        assert (await exchange(api, client_id, code, **extra)).status_code == 400
    code = await code_for(browser, client_id)
    basic = await api.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": VERIFIER,
        },
        auth=(client_id, secret),
    )
    assert basic.status_code == 401  # this client registered for secrets in the body
    assert (await exchange(api, client_id, code, client_secret=secret)).status_code == 200


# ---- hostile assistant web addresses (the one outbound request a stranger can cause) ----

HOSTILE_CLIENT_IDS = [
    "http://example.com/client.json",
    "https://127.0.0.1/client.json",
    "https://[::1]/client.json",
    "https://169.254.169.254/latest/meta-data",
    "https://localhost/client.json",
    "https://2130706433/client.json",
    "https://0x7f000001/client.json",
    "https://example.com:8443/client.json",
    "https://user@example.com/client.json",
    "https://example.com/client.json?x=1",
    "https://example.com/client.json#x",
    "https://example.com",
    "https://example.com/",
    "https://EXAMPLE.com/client.json",
    "https://example.com/../client.json",
    "https://example.com/%2e%2e/client.json",
    "https://example.com\\@evil.example/client.json",
    "https://exämple.com/client.json",
    "https://example.com/" + "a" * 1100,
]


@pytest.mark.parametrize("client_id", HOSTILE_CLIENT_IDS)
def test_hostile_assistant_addresses_are_refused_before_any_lookup(client_id: str) -> None:
    with pytest.raises(MetadataFetchError):
        validate_client_id_url(client_id)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",  # noqa: S104  (a hostile answer to refuse, not an address to bind)
        "10.1.2.3",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "192.168.1.1",
        "198.18.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::1",
        "fc00::1",
        "fd00:ec2::254",
        "fe80::1",
        "2001:db8::1",
    ],
)
def test_private_and_special_answers_are_never_public(address: str) -> None:
    assert not is_public_address(address)


async def test_anonymous_visitors_never_cause_a_fetch(
    make_browser: MakeBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranger sending a web-address client id is sent to sign in; nothing is downloaded."""

    async def fail(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("a lookup was started for an anonymous visitor")

    monkeypatch.setattr("pmsbridge.oauth.metadata_fetch.system_resolver", fail)
    response = await make_browser().get(
        "/oauth/authorize", params=params("https://metadata.example.test/client.json")
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


# ---- staff and anonymous visitors against administrator pages ----

ADMIN_PAGES = [
    ("GET", "/admin/users"),
    ("GET", "/admin/assistants"),
    ("POST", "/admin/users/invite"),
    ("POST", "/admin/users/disable"),
    ("POST", "/admin/users/enable"),
    ("POST", "/admin/users/reset-2fa"),
    ("POST", "/admin/users/role"),
    ("POST", "/admin/users/revoke"),
    ("POST", "/admin/assistants/revoke"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_PAGES)
async def test_staff_cannot_use_administrator_pages(
    browser: httpx.AsyncClient, method: str, path: str
) -> None:
    token = token_in(await browser.get("/account/apps"))
    data = {"csrf_token": token, "user_id": "1", "role": "admin", "email": "x@example.test"}
    response = await browser.request(method, path, data=None if method == "GET" else data)
    assert response.status_code == 403, (method, path)


@pytest.mark.parametrize(("method", "path"), ADMIN_PAGES)
async def test_anonymous_visitors_cannot_use_administrator_pages(
    make_browser: MakeBrowser, method: str, path: str
) -> None:
    response = await make_browser().request(
        method, path, data=None if method == "GET" else {"user_id": "1"}
    )
    assert response.status_code in (303, 401), (method, path)
    assert "admin" not in response.headers.get("location", "").replace("%2Fadmin", "admin") or (
        response.headers["location"].startswith("/login")
    )


async def test_one_person_cannot_cancel_another_persons_approval(
    app: FastAPI, make_browser: MakeBrowser, browser: httpx.AsyncClient
) -> None:
    await create_user("staff-two@example.test", PASSWORD)
    other = make_browser()
    assert (await sign_in(other, email="staff-two@example.test")).status_code == 303
    api = make_browser()
    client_id = await register(api)
    for person in (browser, other):
        assert (await approve(person, params(client_id))).status_code == 303
    token = token_in(await browser.get("/account/apps"))
    response = await browser.post(
        "/account/apps/revoke",
        data={"csrf_token": token, "client_id": client_id, "user_id": "2"},
        headers={"Origin": BASE},
    )
    assert response.status_code == 303
    assert "Example Assistant" in (await other.get("/account/apps")).text
    assert "Example Assistant" not in (await browser.get("/account/apps")).text
