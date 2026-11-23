"""The connection pages: who sees what, adding, replacing, testing, removing, allowing changes."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from conn_web import ADMIN, Browser, Site, make_admin, make_staff
from fastapi import FastAPI
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
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from pmsbridge.accounts import totp
from pmsbridge.accounts.management import ActionRefusedError
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import consent
from pmsbridge.oauth.apps import set_connections
from pmsbridge.oauth.db import OAuthClient, OAuthGrant
from pmsbridge.oauth.grants import Selection
from pmsbridge.pms import connections, http
from pmsbridge.pms.base import PMSAuthError, PMSUpstreamError
from pmsbridge.pms.db import PMSConnection, PMSToken, open_credentials, sealed_credential_columns
from pmsbridge.pms.guesty import auth, provider
from pmsbridge.pms.http import BASE_URLS, Provider

PASSWORD = ADMIN[1]
NEW_ID, NEW_SECRET = "0new-client-id-5678", "new-secret-value-not-real"
LISTINGS = f"{BASE_URLS[Provider.GUESTY]}/listings"


@pytest.fixture
async def boss(browsers: Browser) -> Site:
    site = browsers("203.0.113.5")
    await make_admin(site)
    return site


async def add_through_pages(
    boss: Site, *, label: str = "Main", client_id: str = CLIENT_ID, secret: str = CLIENT_SECRET
) -> httpx.Response:
    await boss.confirm(PASSWORD)
    return await boss.post(
        "/connections/add",
        {"label": label, "client_id": client_id, "client_secret": secret},
        page="/connections/add",
    )


async def count_connections() -> int:
    async with session() as db:
        return len(list((await db.execute(select(PMSConnection.id))).scalars()))


# ---- who sees what ----


async def test_visitors_without_a_session_are_sent_to_sign_in(browsers: Browser) -> None:
    for path in ("/connections", "/connections/add"):
        response = await browsers("203.0.113.9").client.get(path)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")


async def test_staff_see_the_list_but_get_no_controls(browsers: Browser) -> None:
    boss = browsers("203.0.113.5")
    await make_admin(boss)
    connection_id = await make_connection("Beach houses", account_id=ACCOUNT_ID)
    staff = browsers("203.0.113.6")
    await make_staff(staff)
    page = await staff.client.get("/connections")
    assert page.status_code == 200
    assert "Beach houses" in page.text and "Off (read only)" in page.text
    for control in ("Add a Guesty connection", "/connections/test", "Remove", "Allow changes"):
        assert control not in page.text
    assert (await staff.client.get("/connections/add")).status_code == 403
    assert (
        await staff.client.get(f"/connections/rotate?connection={connection_id}")
    ).status_code == 403
    for action in ("add", "rotate", "test", "disable", "enable", "remove", "allow-changes"):
        denied = await staff.post(f"/connections/{action}", {"connection_id": connection_id})
        assert denied.status_code == 403, action
    assert (await read_connection(connection_id)).status == "active"


async def test_an_administrator_sees_the_columns_and_the_controls(boss: Site) -> None:
    connection_id = await make_connection("Beach houses", account_id=ACCOUNT_ID)
    await put_token(connection_id, "t", expires_in=timedelta(hours=5), remaining=3)
    page = (await boss.client.get("/connections")).text
    for expected in (
        "Beach houses",
        "key ending 1234",
        "Working",
        "Off (read only)",
        "3 left",
        "never",
        "Add a Guesty connection",
        "Replace credentials",
        "Allow changes",
    ):
        assert expected in page


async def test_an_empty_list_tells_the_administrator_what_to_do(boss: Site) -> None:
    assert "Add your Guesty account" in (await boss.client.get("/connections")).text


# ---- adding ----


async def test_the_add_form_explains_the_token_and_the_dedicated_key(boss: Site) -> None:
    page = (await boss.client.get("/connections/add")).text
    assert "uses one of the key's 5 daily tokens" in page
    assert "just for pmsbridge" in page and "only once" in page
    assert 'type="password"' in page


async def test_adding_asks_for_confirmation_then_stores_the_validation_token(
    boss: Site, fake_auth: FakeGuestyAuth, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fields = {"label": "Main", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    first = await boss.post("/connections/add", fields, page="/connections/add")
    assert first.status_code == 200 and "Confirm it is you" in first.text
    assert CLIENT_SECRET not in first.text  # never carried through the confirmation page
    assert fake_auth.requests == 0
    assert await count_connections() == 0

    assert boss.secret is not None
    asked = await boss.post(
        "/connections/add",
        {
            "label": "Main",
            "client_id": CLIENT_ID,
            "reauth": "1",
            "password": PASSWORD,
            "code": totp.code_for(boss.secret, totp.current_step() + 1),
        },
        page="/connections/add",
    )
    assert asked.status_code == 200 and "Enter the client secret to finish" in asked.text
    assert 'value="Main"' in asked.text and CLIENT_SECRET not in asked.text

    done = await boss.post("/connections/add", fields, page="/connections/add")
    assert done.status_code == 303 and done.headers["location"] == "/connections?notice=added"
    assert fake_auth.requests == 1  # the validation token is the connection's token
    async with session() as db:
        conn = (await db.execute(select(PMSConnection))).scalar_one()
        assert await db.get(PMSToken, conn.id) is not None
    assert conn.guesty_account_id == ACCOUNT_ID and conn.status == "active"
    assert conn.writes_enabled is False
    assert open_credentials(conn)["client_secret"] == CLIENT_SECRET
    assert CLIENT_SECRET not in caplog.text
    page = (await boss.client.get("/connections?notice=added")).text
    assert "The connection was added." in page and CLIENT_SECRET not in page
    # Using the connection afterwards asks for no second token.
    assert await auth.GuestyTokenManager(
        conn.id, auth.GuestyCredentials(CLIENT_ID, CLIENT_SECRET)
    ).get_token()
    assert fake_auth.requests == 1


async def test_an_existing_key_is_refused_before_any_token_is_requested(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    await make_connection("First", account_id=ACCOUNT_ID)
    response = await add_through_pages(boss, label="Second")
    assert response.status_code == 400 and "already connected" in response.text
    assert fake_auth.requests == 0
    assert await count_connections() == 1


async def test_an_existing_name_is_refused_before_any_token_is_requested(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    await make_connection("Main", client_id="0other-client-id-0000", account_id="other-account")
    response = await add_through_pages(boss)
    assert response.status_code == 400 and "name already exists" in response.text
    assert fake_auth.requests == 0


async def test_another_key_of_a_connected_account_is_refused_and_nothing_is_added(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    await make_connection("First", client_id="0other-client-id-0000", account_id=ACCOUNT_ID)
    response = await add_through_pages(boss, label="Second")
    assert response.status_code == 400 and "Guesty account is already connected" in response.text
    assert await count_connections() == 1


async def test_credentials_guesty_refuses_are_reported_without_echoing_the_secret(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    response = await add_through_pages(boss, secret="wrong-secret-value")  # noqa: S106
    assert (
        response.status_code == 400 and "did not accept the client ID and secret" in response.text
    )
    assert "wrong-secret-value" not in response.text
    assert await count_connections() == 0
    assert f'value="{CLIENT_ID}"' in response.text  # the client ID is kept, the secret is not


@pytest.mark.parametrize(
    ("label", "client_id", "secret"),
    [("", CLIENT_ID, "s"), ("Main", "", "s"), ("Main", CLIENT_ID, ""), ("Main", "has space", "s")],
)
async def test_incomplete_input_is_refused_without_asking_guesty(
    boss: Site, fake_auth: FakeGuestyAuth, label: str, client_id: str, secret: str
) -> None:
    response = await add_through_pages(boss, label=label, client_id=client_id, secret=secret)
    assert response.status_code in (200, 400)
    assert fake_auth.requests == 0 and await count_connections() == 0


async def test_adds_are_limited_per_day(boss: Site, fake_auth: FakeGuestyAuth) -> None:
    for number in range(connections.ADDS_PER_DAY):
        attempt = await add_through_pages(boss, label=f"Try {number}", secret="wrong")  # noqa: S106
        assert "did not accept" in attempt.text
    again = await add_through_pages(boss, label="One more", secret="wrong")  # noqa: S106
    assert again.status_code == 400 and "Too many attempts today" in again.text
    assert fake_auth.requests == connections.ADDS_PER_DAY


# ---- changes need the CSRF token and this site as the origin ----


async def test_changes_without_a_token_or_from_another_site_are_refused(boss: Site) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    for action in ("disable", "test", "stop-changes"):
        without = await boss.post(
            f"/connections/{action}", {"connection_id": connection_id}, csrf=False
        )
        assert without.status_code == 403, action
        foreign = await boss.post(
            f"/connections/{action}",
            {"connection_id": connection_id},
            origin="https://evil.example",
        )
        assert foreign.status_code == 403, action
    assert (await read_connection(connection_id)).status == "active"


async def test_sensitive_actions_need_the_recent_confirmation(boss: Site) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    for action in ("remove", "allow-changes"):
        reply = await boss.post(f"/connections/{action}", {"connection_id": connection_id})
        assert reply.status_code == 200 and "Confirm it is you" in reply.text, action
    conn = await read_connection(connection_id)
    assert conn.writes_enabled is False


# ---- replacing credentials ----


async def approval_for(connection_id: str) -> int:
    async with write_transaction() as db:
        db.add(
            OAuthClient(
                client_id="assistant-client-0123456789",
                client_name="Example Assistant",
                redirect_uris=["https://assistant.example.test/callback"],
                token_endpoint_auth_method="none",  # noqa: S106  (a method name)
                grant_types=["authorization_code"],
                created_at=datetime.now(UTC),
            )
        )
        grant = OAuthGrant(
            user_id=1,
            client_id="assistant-client-0123456789",
            all_connections=False,
            allowed_connection_ids=[connection_id, "another-connection"],
            scope="mcp",
            resource="https://pms.example.test/mcp",
            created_at=datetime.now(UTC),
        )
        db.add(grant)
        await db.flush()
        return grant.id


async def grant_ids(grant_id: int) -> list[str]:
    async with session() as db:
        grant = await db.get(OAuthGrant, grant_id)
        assert grant is not None
        return list(grant.allowed_connection_ids)


async def test_replacing_credentials_keeps_the_id_and_the_approvals(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    fake_auth.add_account(NEW_ID, NEW_SECRET, ACCOUNT_ID)
    connection_id = await make_connection("Main", status="needs_attention", account_id=ACCOUNT_ID)
    grant = await approval_for(connection_id)
    form = (await boss.client.get(f"/connections/rotate?connection={connection_id}")).text
    assert "Replace the credentials of Main" in form and f'value="{connection_id}"' in form
    await boss.confirm(PASSWORD)
    done = await boss.post(
        "/connections/rotate",
        {"connection_id": connection_id, "client_id": NEW_ID, "client_secret": NEW_SECRET},
        page=f"/connections/rotate?connection={connection_id}",
    )
    assert done.status_code == 303 and done.headers["location"] == "/connections?notice=replaced"
    conn = await read_connection(connection_id)
    assert conn.id == connection_id and conn.status == "active"
    assert open_credentials(conn) == {"client_id": NEW_ID, "client_secret": NEW_SECRET}
    assert await read_token_row(connection_id) is not None
    assert await grant_ids(grant) == [connection_id, "another-connection"]
    assert fake_auth.requests == 1


async def test_wrong_new_credentials_leave_the_old_ones_working(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    await boss.confirm(PASSWORD)
    failed = await boss.post(
        "/connections/rotate",
        {"connection_id": connection_id, "client_id": CLIENT_ID, "client_secret": "nope-nope"},
        page=f"/connections/rotate?connection={connection_id}",
    )
    assert failed.status_code == 400 and "nope-nope" not in failed.text
    assert open_credentials(await read_connection(connection_id))["client_secret"] == CLIENT_SECRET


async def test_rotating_a_key_used_by_another_connection_is_refused_without_a_request(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    await make_connection("Other", client_id=NEW_ID, account_id="another-account")
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    await boss.confirm(PASSWORD)
    refused = await boss.post(
        "/connections/rotate",
        {"connection_id": connection_id, "client_id": NEW_ID, "client_secret": NEW_SECRET},
        page=f"/connections/rotate?connection={connection_id}",
    )
    assert refused.status_code == 400 and "already used by another connection" in refused.text
    assert fake_auth.requests == 0


async def test_replacements_are_limited_per_connection(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    await boss.confirm(PASSWORD)
    reply = httpx.Response(200)
    for _ in range(connections.REPLACEMENTS_PER_DAY + 1):
        reply = await boss.post(
            "/connections/rotate",
            {"connection_id": connection_id, "client_id": CLIENT_ID, "client_secret": "bad-secret"},
            page=f"/connections/rotate?connection={connection_id}",
        )
    assert "Too many attempts today" in reply.text
    assert fake_auth.requests == connections.REPLACEMENTS_PER_DAY


# ---- testing never asks for a token ----


async def test_testing_a_connection_without_a_usable_token_requests_nothing(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    reply = await boss.post("/connections/test", {"connection_id": connection_id})
    assert "will be fetched the next time" in reply.text
    assert fake_auth.requests == 0
    await put_token(connection_id, "old", expires_in=timedelta(seconds=10))
    reply = await boss.post("/connections/test", {"connection_id": connection_id})
    assert "will be fetched the next time" in reply.text
    assert fake_auth.requests == 0


async def test_testing_uses_the_stored_token_for_one_read_only_call(
    boss: Site, fake_auth: FakeGuestyAuth, mock_guesty: respx.MockRouter
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    token = make_jwt(CLIENT_ID, ACCOUNT_ID)
    await put_token(connection_id, token, expires_in=timedelta(hours=5))
    route = mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(200, json={"results": []}))
    reply = await boss.post("/connections/test", {"connection_id": connection_id})
    assert "Guesty answered. The connection works." in reply.text
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == f"Bearer {token}"
    assert fake_auth.requests == 0
    assert (await read_connection(connection_id)).last_success_at is not None


async def test_a_rejected_token_in_a_test_is_reported_and_not_replaced(
    boss: Site, fake_auth: FakeGuestyAuth, mock_guesty: respx.MockRouter
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    await put_token(connection_id, make_jwt(CLIENT_ID, ACCOUNT_ID), expires_in=timedelta(hours=5))
    mock_guesty.get(LISTINGS).mock(return_value=httpx.Response(401, json={"message": "BODY-LEAK"}))
    reply = await boss.post("/connections/test", {"connection_id": connection_id})
    assert "did not accept the check" in reply.text and "No new token was requested" in reply.text
    assert "BODY-LEAK" not in reply.text
    assert fake_auth.requests == 0


# ---- turning off and on, allowing changes ----


async def test_turning_a_connection_off_and_on(boss: Site) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    off = await boss.post("/connections/disable", {"connection_id": connection_id})
    assert off.status_code == 303
    assert (await read_connection(connection_id)).status == "disabled"
    assert "Turned off" in (await boss.client.get("/connections")).text
    await boss.post("/connections/enable", {"connection_id": connection_id})
    assert (await read_connection(connection_id)).status == "active"


async def test_allowing_changes_shows_the_warning_then_needs_the_confirmation(boss: Site) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    warning = (await boss.client.get(f"/connections/allow-changes?connection={connection_id}")).text
    assert "prices, availability, guest messages" in warning
    assert "You are responsible for what it does" in warning
    await boss.confirm(PASSWORD)
    done = await boss.post(
        "/connections/allow-changes",
        {"connection_id": connection_id},
        page=f"/connections/allow-changes?connection={connection_id}",
    )
    assert done.status_code == 303
    assert (await read_connection(connection_id)).writes_enabled is True
    assert "Allowed" in (await boss.client.get("/connections")).text
    stopped = await boss.post("/connections/stop-changes", {"connection_id": connection_id})
    assert stopped.status_code == 303
    assert (await read_connection(connection_id)).writes_enabled is False


async def test_a_new_connection_has_changes_off(boss: Site, fake_auth: FakeGuestyAuth) -> None:
    await add_through_pages(boss)
    async with session() as db:
        assert (await db.execute(select(PMSConnection))).scalar_one().writes_enabled is False


# ---- removing ----


async def test_removing_deletes_everything_and_takes_the_id_off_approvals(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    await put_token(connection_id, "t", expires_in=timedelta(hours=5))
    grant = await approval_for(connection_id)
    page = (await boss.client.get(f"/connections/remove?connection={connection_id}")).text
    assert "deletes the connection" in page
    await boss.confirm(PASSWORD)
    done = await boss.post(
        "/connections/remove",
        {"connection_id": connection_id},
        page=f"/connections/remove?connection={connection_id}",
    )
    assert done.status_code == 303 and done.headers["location"] == "/connections?notice=removed"
    async with session() as db:
        assert await db.get(PMSConnection, connection_id) is None
        assert await db.get(PMSToken, connection_id) is None
    assert await grant_ids(grant) == ["another-connection"]


async def test_unknown_connection_ids_are_refused(boss: Site) -> None:
    missing = "0" * 32
    assert (await boss.post("/connections/disable", {"connection_id": missing})).status_code == 400
    assert (await boss.post("/connections/disable", {"connection_id": "../x"})).status_code == 400
    assert (await boss.client.get(f"/connections/remove?connection={missing}")).status_code == 404
    assert (await boss.client.get("/connections/rotate?connection=nope")).status_code == 404


# ---- the consent screen ----


async def test_the_consent_screen_offers_the_working_connections_only(app: FastAPI) -> None:
    await make_connection("Beach houses", client_id="0a-client-id-00000000")
    await make_connection(
        "City flats", client_id="0b-client-id-00000000", status="token_budget_low"
    )
    await make_connection("Broken", client_id="0c-client-id-00000000", status="needs_attention")
    await make_connection("Paused", client_id="0d-client-id-00000000", status="disabled")
    choices = await consent.choices_for(1)
    assert [choice.label for choice in choices] == ["Beach houses", "City flats"]
    assert all(len(choice.id) == 32 for choice in choices)


# ---- the application itself ----


async def test_the_shared_http_clients_are_closed_when_the_application_stops(app: FastAPI) -> None:
    client = http.get_http_client(Provider.GUESTY)
    async with app.router.lifespan_context(app):
        assert http._clients  # pyright: ignore[reportPrivateUsage]
    assert not http._clients  # pyright: ignore[reportPrivateUsage]
    assert http.get_http_client(Provider.GUESTY) is not client


async def test_the_guesty_domains_are_discovered_at_startup(app: FastAPI) -> None:
    assert provider._loaded is not None  # pyright: ignore[reportPrivateUsage]


async def test_replacing_credentials_of_an_unknown_connection_is_a_clear_error(boss: Site) -> None:
    await boss.confirm(PASSWORD)
    reply = await boss.post(
        "/connections/rotate",
        {"connection_id": "0" * 32, "client_id": NEW_ID, "client_secret": NEW_SECRET},
    )
    assert reply.status_code == 404


async def test_other_systems_are_not_handled_by_the_guesty_actions(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    connection_id = await make_connection("Other", provider="hostaway")
    for action in ("test",):
        reply = await boss.post(f"/connections/{action}", {"connection_id": connection_id})
        assert reply.status_code == 400 and "only available for Guesty" in reply.text
    assert fake_auth.requests == 0


# ---- concurrency, switch rules, notices, save failures ----


async def test_two_concurrent_adds_make_one_token_request(
    boss: Site, fake_auth: FakeGuestyAuth
) -> None:
    await boss.confirm(PASSWORD)
    fields = {"label": "Main", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    first, second = await asyncio.gather(
        boss.post("/connections/add", fields, page="/connections/add"),
        boss.post("/connections/add", fields, page="/connections/add"),
    )
    assert sorted([first.status_code, second.status_code]) == [303, 400]
    assert fake_auth.requests == 1
    assert await count_connections() == 1


async def test_two_concurrent_identical_replacements_make_one_token_request(
    fake_auth: FakeGuestyAuth,
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    await asyncio.gather(
        connections.replace_guesty_credentials(connection_id, CLIENT_ID, CLIENT_SECRET, 1),
        connections.replace_guesty_credentials(connection_id, CLIENT_ID, CLIENT_SECRET, 1),
    )
    assert fake_auth.requests == 1
    assert await read_token_row(connection_id) is not None
    await connections.replace_guesty_credentials(connection_id, CLIENT_ID, CLIENT_SECRET, 1)
    assert fake_auth.requests == 1  # the same credentials again: nothing to do


async def test_a_key_taken_while_waiting_for_the_lock_is_refused_without_a_request(
    fake_auth: FakeGuestyAuth,
) -> None:
    await make_connection("Other", client_id=NEW_ID, account_id="another-account")
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    with pytest.raises(PMSAuthError):
        await auth.replace_credentials(connection_id, NEW_ID, NEW_SECRET)
    assert fake_auth.requests == 0


async def test_only_a_turned_off_connection_can_be_turned_on(boss: Site) -> None:
    broken = await make_connection(
        "Broken", client_id="0b-client-id-00000000", status="needs_attention"
    )
    paused = await make_connection("Paused", client_id="0c-client-id-00000000", status="disabled")
    working = await make_connection("Working", client_id="0d-client-id-00000000")
    reply = await boss.post("/connections/enable", {"connection_id": broken})
    assert reply.status_code == 400 and "Replace the credentials first" in reply.text
    assert (await read_connection(broken)).status == "needs_attention"
    reply = await boss.post("/connections/disable", {"connection_id": broken})
    assert reply.status_code == 400 and "needs attention" in reply.text
    assert (await read_connection(broken)).status == "needs_attention"
    assert (await boss.post("/connections/enable", {"connection_id": paused})).status_code == 303
    assert (await read_connection(paused)).status == "active"
    assert (await boss.post("/connections/enable", {"connection_id": working})).status_code == 400


async def test_only_known_notice_keys_are_shown(boss: Site) -> None:
    page = (await boss.client.get("/connections?notice=Your+account+is+locked%3A+call+evil")).text
    assert "locked" not in page and "evil" not in page and 'role="status"' not in page
    assert "The connection was added." in (await boss.client.get("/connections?notice=added")).text


async def test_a_test_result_is_shown_from_the_server_not_the_address(
    boss: Site, fake_auth: FakeGuestyAuth, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    reply = await boss.post("/connections/test", {"connection_id": connection_id})
    assert "will be fetched the next time" in reply.text
    audit = [r for r in caplog.records if r.getMessage() == "connection tested"]
    assert audit and getattr(audit[0], "outcome") == "ok"  # noqa: B009
    assert connection_id == getattr(audit[0], "connection_id")  # noqa: B009


async def test_a_token_for_a_replaced_key_is_discarded_not_stored(
    fake_auth: FakeGuestyAuth,
) -> None:
    fake_auth.add_account(NEW_ID, NEW_SECRET, ACCOUNT_ID)
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    old_manager = auth.GuestyTokenManager(
        connection_id, auth.GuestyCredentials(CLIENT_ID, CLIENT_SECRET)
    )
    # Another process (a command) replaces the credentials while the old manager is mid-request.
    gate = asyncio.Event()
    real = auth._request_token  # pyright: ignore[reportPrivateUsage]

    async def slow(credentials: auth.GuestyCredentials) -> auth.IssuedToken:
        issued = await real(credentials)
        gate.set()
        await asyncio.sleep(0.2)
        return issued

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "_request_token", slow)
        pending = asyncio.create_task(old_manager.get_token())
        await asyncio.wait_for(gate.wait(), 5)
        async with write_transaction() as db:
            conn = await db.get(PMSConnection, connection_id)
            assert conn is not None
            for column, value in sealed_credential_columns(
                connection_id, {"client_id": NEW_ID, "client_secret": NEW_SECRET}
            ).items():
                setattr(conn, column, value)
        with pytest.raises(PMSUpstreamError) as error:
            await pending
    assert "replaced" in error.value.message
    assert await read_token_row(connection_id) is None
    assert connection_id not in auth._unsaved  # pyright: ignore[reportPrivateUsage]
    assert (await read_connection(connection_id)).status == "active"


async def test_a_failed_save_after_validation_says_a_token_was_used(
    boss: Site, fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> object:
        raise OperationalError("insert", {}, Exception("disk I/O error SECRET-DETAIL"))

    monkeypatch.setattr(connections, "write_transaction", broken)
    reply = await add_through_pages(boss)
    assert reply.status_code == 400
    assert "could not be saved; one daily token was used" in reply.text
    assert "SECRET-DETAIL" not in reply.text


async def test_a_failed_save_of_replacement_credentials_says_a_token_was_used(
    fake_auth: FakeGuestyAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_auth.add_account(NEW_ID, NEW_SECRET, ACCOUNT_ID)
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)

    def broken() -> object:
        raise OperationalError("update", {}, Exception("disk I/O error"))

    monkeypatch.setattr(auth, "write_transaction", broken)
    with pytest.raises(ActionRefusedError) as error:
        await connections.replace_guesty_credentials(connection_id, NEW_ID, NEW_SECRET, 1)
    assert "one daily token was used" in str(error.value)
    assert fake_auth.requests == 1
    assert open_credentials(await read_connection(connection_id))["client_id"] == CLIENT_ID


async def test_the_confirmation_page_names_the_connection(boss: Site) -> None:
    connection_id = await make_connection("Beach houses", account_id=ACCOUNT_ID)
    for action, words in (("remove", "Remove on"), ("allow-changes", "Allow changes on")):
        reply = await boss.post(f"/connections/{action}", {"connection_id": connection_id})
        assert f"{words} Beach houses" in reply.text, action


async def test_the_list_is_capped(boss: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connections, "MAX_LISTED", 2)
    for number in range(3):
        await make_connection(f"Place {number}", client_id=f"0client-id-{number:08d}")
    page = (await boss.client.get("/connections")).text
    assert "Place 0" in page and "Place 1" in page and "Place 2" not in page
    assert "Showing the first 2 connections only" in page


@pytest.mark.parametrize("action", ["add", "rotate", "remove", "allow-changes"])
async def test_sensitive_posts_without_a_token_or_from_another_site_change_nothing(
    boss: Site, fake_auth: FakeGuestyAuth, action: str
) -> None:
    connection_id = await make_connection("Main", account_id=ACCOUNT_ID)
    await boss.confirm(PASSWORD)  # even with a recent confirmation
    data = {
        "connection_id": connection_id,
        "label": "New",
        "client_id": NEW_ID,
        "client_secret": NEW_SECRET,
    }
    assert (await boss.post(f"/connections/{action}", data, csrf=False)).status_code == 403
    foreign = await boss.post(f"/connections/{action}", data, origin="https://evil.example")
    assert foreign.status_code == 403
    assert fake_auth.requests == 0
    conn = await read_connection(connection_id)
    assert conn.writes_enabled is False and conn.label == "Main"
    assert open_credentials(conn)["client_id"] == CLIENT_ID
    assert await count_connections() == 1


async def test_changing_an_assistants_connections_keeps_the_ones_not_offered(
    boss: Site,
) -> None:
    paused = await make_connection("Paused", client_id="0p-client-id-00000000", status="disabled")
    working = await make_connection("Working", client_id="0w-client-id-00000000")
    other = await make_connection("Other", client_id="0o-client-id-00000000")
    grant = await approval_for(paused)
    async with write_transaction() as db:
        row = await db.get(OAuthGrant, grant)
        assert row is not None
        row.allowed_connection_ids = [paused, other]
    changed = await set_connections(
        boss.user_id, "assistant-client-0123456789", Selection(False, [working])
    )
    assert changed == 1
    assert await grant_ids(grant) == [working, paused]
