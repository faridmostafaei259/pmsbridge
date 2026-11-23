"""The connected-apps page: listing, revoking and choosing connections."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from oauth_helpers import (
    BASE_URL,
    connect,
    get_code,
    page_token,
    register_client,
    token_request,
)
from sqlalchemy import select, update

from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import consent
from pmsbridge.oauth.apps import list_approvals
from pmsbridge.oauth.db import OAuthGrant, OAuthToken
from pmsbridge.oauth.tokens import validate_access_token


@pytest.fixture
async def api(make_browser: object) -> httpx.AsyncClient:
    return make_browser("198.51.100.7")  # type: ignore[operator, no-any-return]


def offer_two_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install two connections after the approval was given (it then covers none)."""

    async def source(_user_id: int) -> Sequence[consent.ConnectionChoice]:
        return [
            consent.ConnectionChoice("conn-a", "Beach houses"),
            consent.ConnectionChoice("conn-b", "City flats"),
        ]

    monkeypatch.setattr(consent, "_sources", [source])


async def post(
    browser: httpx.AsyncClient, path: str, data: dict[str, str] | list[tuple[str, str]]
) -> httpx.Response:
    page = await browser.get("/account/apps")
    token = page_token(page.text)
    fields = [("csrf_token", token)] + (list(data.items()) if isinstance(data, dict) else data)
    return await browser.post(
        path,
        content=urlencode(fields),
        headers={"Origin": BASE_URL, "Content-Type": "application/x-www-form-urlencoded"},
    )


async def grants() -> list[OAuthGrant]:
    async with session() as db:
        return list((await db.execute(select(OAuthGrant).order_by(OAuthGrant.id))).scalars())


async def test_the_page_needs_a_signed_in_person(anonymous: httpx.AsyncClient) -> None:
    response = await anonymous.get("/account/apps")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_an_empty_page_says_so(browser: httpx.AsyncClient) -> None:
    page = await browser.get("/account/apps")
    assert page.status_code == 200
    assert "You have not approved any assistants" in page.text


async def test_the_page_shows_how_to_connect_with_the_server_address(
    browser: httpx.AsyncClient,
) -> None:
    page = (await browser.get("/account/apps")).text
    assert "Connect an AI assistant" in page
    assert f"<code>{BASE_URL}/mcp</code>" in page
    assert "custom connector" in page and "Allow access" in page


async def test_the_page_lists_an_assistant_once_with_its_details(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, _ = await connect(browser, api)
    # Approving the same assistant again adds a grant but not a second entry.
    await get_code(browser, client_id)
    page = await browser.get("/account/apps")
    assert page.text.count("Example Assistant") == 1
    assert "assistant.example.test" in page.text
    assert "No connections" in page.text
    assert "No PMS connections yet" in page.text
    assert "Last used" in page.text and "never" not in page.text.split("Last used")[1][:40]


async def test_revoking_ends_every_token_of_that_assistant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    other_id, other = await connect(browser, api)
    response = await post(browser, "/account/apps/revoke", {"client_id": client_id})
    assert response.status_code == 303
    assert await validate_access_token(body["access_token"]) is None
    assert await validate_access_token(other["access_token"]) is not None
    refresh = await token_request(
        api,
        {
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refresh.json()["error"] == "invalid_grant"
    page = await browser.get("/account/apps?notice=revoked")
    assert "Access was cancelled" in page.text
    live = {grant.client_id: grant.revoked_at is None for grant in await grants()}
    assert live == {client_id: False, other_id: True}


async def test_changes_need_the_csrf_token_and_this_sites_origin(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, body = await connect(browser, api)
    page = await browser.get("/account/apps")
    token = page_token(page.text)
    no_token = await browser.post(
        "/account/apps/revoke", data={"client_id": client_id}, headers={"Origin": BASE_URL}
    )
    wrong_origin = await browser.post(
        "/account/apps/revoke",
        data={"client_id": client_id, "csrf_token": token},
        headers={"Origin": "https://evil.example"},
    )
    assert (no_token.status_code, wrong_origin.status_code) == (403, 403)
    assert await validate_access_token(body["access_token"]) is not None


async def test_a_malformed_client_id_is_refused(browser: httpx.AsyncClient) -> None:
    response = await post(browser, "/account/apps/revoke", {"client_id": "../x"})
    assert response.status_code == 400


async def test_choosing_connections_changes_the_grant(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id, _ = await connect(browser, api)
    offer_two_connections(monkeypatch)
    page = await browser.get("/account/apps")
    assert "Beach houses" in page.text and "City flats" in page.text
    response = await post(
        browser,
        "/account/apps/connections",
        [("client_id", client_id), ("mode", "selected"), ("connection", "conn-b")],
    )
    assert response.status_code == 303
    (grant,) = await grants()
    assert (grant.all_connections, grant.allowed_connection_ids) == (False, ["conn-b"])
    page = await browser.get("/account/apps")
    assert "City flats" in page.text.split("<th>Connections</th>")[1].split("</td>")[0]
    await post(browser, "/account/apps/connections", [("client_id", client_id), ("mode", "all")])
    (grant,) = await grants()
    assert grant.all_connections is True


async def test_unknown_or_empty_choices_are_refused(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id, _ = await connect(browser, api)
    offer_two_connections(monkeypatch)
    for fields in (
        [("client_id", client_id), ("mode", "selected"), ("connection", "not-mine")],
        [("client_id", client_id), ("mode", "selected")],
        [("client_id", client_id)],
    ):
        response = await post(browser, "/account/apps/connections", fields)
        assert response.status_code == 400
    (grant,) = await grants()
    assert grant.allowed_connection_ids == []


async def test_choices_are_refused_while_no_connections_exist(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    client_id, _ = await connect(browser, api)
    response = await post(
        browser, "/account/apps/connections", [("client_id", client_id), ("mode", "all")]
    )
    assert response.status_code == 400
    (grant,) = await grants()
    assert grant.all_connections is False


async def test_choices_for_an_assistant_that_is_not_approved_fail(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    offer_two_connections(monkeypatch)
    stranger = str((await register_client(api))["client_id"])
    response = await post(
        browser, "/account/apps/connections", [("client_id", stranger), ("mode", "all")]
    )
    assert response.status_code == 400
    assert "no longer approved" in response.text
    assert await grants() == []


async def test_last_used_survives_the_cleanup_of_expired_tokens(
    browser: httpx.AsyncClient, api: httpx.AsyncClient
) -> None:
    first_id, _ = await connect(browser, api)
    async with write_transaction() as db:
        await db.execute(
            update(OAuthToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    # Another assistant's issuance deletes every expired token, including the first one's.
    await connect(browser, api)
    async with session() as db:
        left = list((await db.execute(select(OAuthToken.client_id))).scalars())
    assert first_id not in left
    approvals, _ = await list_approvals()
    by_client = {item.client_id: item for item in approvals}
    used = by_client[first_id].last_used_at
    assert used is not None and datetime.now(UTC) - used < timedelta(minutes=1)
    page = await browser.get("/account/apps")
    assert "never" not in page.text
    assert used.strftime("%Y-%m-%d") in page.text
