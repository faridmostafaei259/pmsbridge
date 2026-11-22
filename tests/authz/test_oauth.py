"""Who may authorize an AI assistant, and what stops a grant from outliving its person."""

import base64
import hashlib
import re
from collections.abc import AsyncIterator, Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select, update

from pmsbridge.accounts import management, passwords, totp, twofactor
from pmsbridge.accounts.db import User
from pmsbridge.accounts.service import create_user
from pmsbridge.admin import users as admin_users
from pmsbridge.app import create_app
from pmsbridge.core.crypto import generate_key
from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.core.storage import reset_database, session, write_transaction
from pmsbridge.oauth.db import OAuthGrant
from pmsbridge.oauth.grants import redeem_code

BASE_URL = "https://pms.example.test"
CALLBACK = "https://assistant.example.test/callback"
VERIFIER = "fake-verifier-0123456789-0123456789-0123456789-abcdef"
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
STAFF = ("staff@example.test", "fake staff passphrase")
OTHER = ("other@example.test", "fake other passphrase")
ADMIN = ("admin@example.test", "fake admin passphrase")
SECOND_ADMIN = ("admin2@example.test", "fake second admin passphrase")
ADMIN_SECRET = bytes(range(20))


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("ENCRYPTION_KEYS", generate_key("test"))
    clear_settings_cache()
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(passwords, "_dummy_hash", None)


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    yield create_app(("pmsbridge.core.web", "pmsbridge.accounts.web", "pmsbridge.oauth.web"))
    await reset_database()


Browser = Callable[[], httpx.AsyncClient]


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[Browser]:
    opened: list[httpx.AsyncClient] = []

    def new() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.5", 1)), base_url=BASE_URL
        )
        opened.append(client)
        return client

    yield new
    for client in opened:
        await client.aclose()


@pytest.fixture
async def accounts(app: FastAPI) -> dict[str, int]:
    admin = await create_user(*ADMIN, role="admin")
    second = await create_user(*SECOND_ADMIN, role="admin")
    async with write_transaction() as db:
        twofactor.add_totp(db, admin, ADMIN_SECRET, 0)
        twofactor.add_totp(db, second, ADMIN_SECRET, 0)
    return {
        "staff": await create_user(*STAFF),
        "other": await create_user(*OTHER),
        "admin": admin,
        "admin2": second,
    }


async def sign_in(
    client: httpx.AsyncClient, credentials: tuple[str, str], secret: bytes | None = None
) -> httpx.Response:
    page = await client.get("/login")
    token = CSRF_FIELD.search(page.text)
    assert token
    response = await client.post(
        "/login",
        data={"email": credentials[0], "password": credentials[1], "csrf_token": token.group(1)},
        headers={"Origin": BASE_URL},
    )
    if secret is None:
        return response
    token = CSRF_FIELD.search((await client.get("/login/verify")).text)
    assert token
    return await client.post(
        "/login/verify",
        data={"code": totp.code_for(secret, totp.current_step()), "csrf_token": token.group(1)},
        headers={"Origin": BASE_URL},
    )


async def new_client(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/oauth/register",
        json={
            "client_name": "Example Assistant",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201
    return str(response.json()["client_id"])


def params_for(client_id: str) -> dict[str, str]:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode()
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge.rstrip("="),
        "code_challenge_method": "S256",
        "state": "s",
    }


async def authorize(client: httpx.AsyncClient, client_id: str) -> str:
    """Approve on the consent page and return the code."""
    params = params_for(client_id)
    page = await client.get("/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    token = CSRF_FIELD.search(page.text)
    assert token
    response = await client.post(
        "/oauth/authorize",
        data={**params, "decision": "approve", "csrf_token": token.group(1)},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


async def grants_of(user_id: int) -> list[OAuthGrant]:
    async with session() as db:
        rows = await db.execute(select(OAuthGrant).where(OAuthGrant.user_id == user_id))
        return list(rows.scalars())


async def test_staff_and_administrators_may_authorize_assistants(
    browser: Browser, accounts: dict[str, int]
) -> None:
    staff, admin = browser(), browser()
    await sign_in(staff, STAFF)
    await sign_in(admin, ADMIN, ADMIN_SECRET)
    client_id = await new_client(staff)
    assert await authorize(staff, client_id)
    assert await authorize(admin, client_id)
    assert len(await grants_of(accounts["staff"])) == 1
    assert len(await grants_of(accounts["admin"])) == 1


async def test_a_grant_belongs_to_the_person_who_approved_it(
    browser: Browser, accounts: dict[str, int]
) -> None:
    one, two = browser(), browser()
    await sign_in(one, STAFF)
    await sign_in(two, OTHER)
    client_id = await new_client(one)
    code = await authorize(one, client_id)
    redeemed = await redeem_code(code, client_id, CALLBACK, VERIFIER)
    assert redeemed is not None and redeemed.user_id == accounts["staff"]
    assert await grants_of(accounts["other"]) == []


async def test_a_signed_out_visitor_is_sent_to_sign_in_and_gets_no_consent_page(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    client_id = await new_client(client)
    response = await client.get("/oauth/authorize", params=params_for(client_id))
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    posted = await client.post(
        "/oauth/authorize",
        data={**params_for(client_id), "decision": "approve"},
        headers={"Origin": BASE_URL},
    )
    assert posted.status_code == 401
    assert "location" not in posted.headers


async def test_an_administrator_without_two_factor_never_reaches_the_consent_page(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, ADMIN, ADMIN_SECRET)
    client_id = await new_client(client)
    async with write_transaction() as db:
        await twofactor.remove_two_factor(db, accounts["admin"])
    response = await client.get("/oauth/authorize", params=params_for(client_id))
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert await grants_of(accounts["admin"]) == []


async def test_another_persons_form_token_does_not_approve(
    browser: Browser, accounts: dict[str, int]
) -> None:
    one, two = browser(), browser()
    await sign_in(one, STAFF)
    await sign_in(two, OTHER)
    client_id = await new_client(one)
    params = params_for(client_id)
    token = CSRF_FIELD.search((await one.get("/oauth/authorize", params=params)).text)
    assert token
    response = await two.post(
        "/oauth/authorize",
        data={**params, "decision": "approve", "csrf_token": token.group(1)},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 403
    assert await grants_of(accounts["other"]) == []


@pytest.mark.parametrize("action", ["disable", "reset_two_factor", "promote", "demote"])
async def test_grants_are_revoked_when_the_person_is_changed(
    browser: Browser, accounts: dict[str, int], action: str
) -> None:
    """A disabled, reset, promoted or demoted person keeps no assistant access."""
    target = accounts["admin2"] if action == "demote" else accounts["staff"]
    client = browser()
    credentials, secret = (SECOND_ADMIN, ADMIN_SECRET) if action == "demote" else (STAFF, None)
    if action == "reset_two_factor":
        async with write_transaction() as db:
            twofactor.add_totp(db, target, ADMIN_SECRET, 0)
        secret = ADMIN_SECRET
    await sign_in(client, credentials, secret)
    client_id = await new_client(client)
    code = await authorize(client, client_id)
    other_client = browser()
    await sign_in(other_client, OTHER)
    other_code = await authorize(other_client, client_id)
    actor = accounts["admin"]
    if action == "disable":
        await management.disable_user(target, actor)
    elif action == "reset_two_factor":
        await management.reset_two_factor(target, actor)
    elif action == "promote":
        await management.change_role(target, "admin", actor)
    else:
        await management.change_role(target, "staff", actor)
    grants = await grants_of(target)
    assert grants and all(grant.revoked_at is not None for grant in grants)
    assert await redeem_code(code, client_id, CALLBACK, VERIFIER) is None
    # Other people's grants are untouched.
    assert await redeem_code(other_code, client_id, CALLBACK, VERIFIER) is not None


async def test_a_password_reset_revokes_the_persons_grants(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, SECOND_ADMIN, ADMIN_SECRET)
    client_id = await new_client(client)
    code = await authorize(client, client_id)
    link = await admin_users.reset_admin_password(SECOND_ADMIN[0])
    parts = urlsplit(link)
    fresh = browser()
    assert (await fresh.get(parts.path, params=parse_qs(parts.query))).status_code == 303
    page = await fresh.get(parts.path)
    token = CSRF_FIELD.search(page.text)
    assert token
    done = await fresh.post(
        parts.path,
        data={
            "password": "a brand new fake passphrase",
            "confirm": "a brand new fake passphrase",
            "csrf_token": token.group(1),
        },
        headers={"Origin": BASE_URL},
    )
    assert done.status_code == 200
    grants = await grants_of(accounts["admin2"])
    assert grants and all(grant.revoked_at is not None for grant in grants)
    assert await redeem_code(code, client_id, CALLBACK, VERIFIER) is None


async def test_a_disabled_person_cannot_open_the_consent_page(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    await sign_in(client, STAFF)
    client_id = await new_client(client)
    async with write_transaction() as db:
        await db.execute(update(User).where(User.id == accounts["staff"]).values(is_active=False))
    response = await client.get("/oauth/authorize", params=params_for(client_id))
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_registering_a_client_gives_no_access_by_itself(
    browser: Browser, accounts: dict[str, int]
) -> None:
    client = browser()
    client_id = await new_client(client)
    async with session() as db:
        assert (await db.execute(select(OAuthGrant))).first() is None
    assert client_id
