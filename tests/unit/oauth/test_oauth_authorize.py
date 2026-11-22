import html
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx
import pytest
from oauth_helpers import (
    BASE_URL,
    CALLBACK,
    EMAIL,
    PASSWORD,
    approve,
    authorize_params,
    challenge_for,
    link_query,
    location_query,
    page_token,
    register_client,
    sign_in,
)
from sqlalchemy import select, update

from pmsbridge.accounts.pages import PAGE_CSP
from pmsbridge.accounts.sessions import hash_token
from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import consent
from pmsbridge.oauth.db import OAuthClient, OAuthCode, OAuthGrant


@pytest.fixture(autouse=True)
def _default_choices() -> Iterator[None]:
    yield
    consent.set_choice_source(consent._no_connections)  # pyright: ignore[reportPrivateUsage]


async def client_id_of(client: httpx.AsyncClient, **overrides: object) -> str:
    return str((await register_client(client, **overrides))["client_id"])


# ---- Checks before the client and the redirect URI are known: never redirect ----


@pytest.mark.parametrize("signed_in", [False, True])
async def test_an_unknown_client_gets_an_error_page_and_no_redirect(
    anonymous: httpx.AsyncClient, person: int, signed_in: bool
) -> None:
    if signed_in:
        await sign_in(anonymous)
    response = await anonymous.get("/oauth/authorize", params=authorize_params("x" * 32, state="s"))
    assert response.status_code == 400
    assert "location" not in response.headers
    assert "not registered here" in response.text


async def test_a_missing_client_id_gets_an_error_page(anonymous: httpx.AsyncClient) -> None:
    params = authorize_params("unused")
    del params["client_id"]
    response = await anonymous.get("/oauth/authorize", params=params)
    assert response.status_code == 400
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://evil.example/callback",
        CALLBACK + "/",
        CALLBACK + "?x=1",
        "https://assistant.example.test:444/callback",
        "http://assistant.example.test/callback",
        "",
        "javascript:alert(1)",
    ],
)
async def test_a_redirect_uri_that_was_not_registered_is_never_redirected_to(
    anonymous: httpx.AsyncClient, redirect_uri: str
) -> None:
    client_id = await client_id_of(anonymous)
    response = await anonymous.get(
        "/oauth/authorize",
        params=authorize_params(client_id, redirect_uri=redirect_uri, response_type="token"),
    )
    assert response.status_code == 400
    assert "location" not in response.headers
    assert "did not register" in response.text


async def test_the_redirect_uri_is_required_even_with_one_registration(
    anonymous: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(anonymous)
    params = authorize_params(client_id)
    del params["redirect_uri"]
    response = await anonymous.get("/oauth/authorize", params=params)
    assert response.status_code == 400
    assert "location" not in response.headers


async def test_a_repeated_client_id_or_redirect_uri_is_not_redirected(
    anonymous: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(anonymous)
    for name, other in (("client_id", "y" * 32), ("redirect_uri", "https://evil.example/cb")):
        query = "&".join(
            f"{key}={quote(value, safe='')}" for key, value in authorize_params(client_id).items()
        )
        response = await anonymous.get(f"/oauth/authorize?{query}&{name}={quote(other, safe='')}")
        assert response.status_code == 400, name
        assert "location" not in response.headers


async def test_an_expired_registration_is_an_error_page(anonymous: httpx.AsyncClient) -> None:
    client_id = await client_id_of(anonymous)
    async with write_transaction() as db:
        await db.execute(
            update(OAuthClient).values(created_at=datetime.now(UTC) - timedelta(hours=30))
        )
    response = await anonymous.get("/oauth/authorize", params=authorize_params(client_id))
    assert response.status_code == 400
    assert "location" not in response.headers


async def test_an_overlong_request_is_an_error_page(anonymous: httpx.AsyncClient) -> None:
    client_id = await client_id_of(anonymous)
    response = await anonymous.get(
        "/oauth/authorize", params=authorize_params(client_id, state="s" * 2500)
    )
    assert response.status_code == 400
    assert "location" not in response.headers


# ---- Request errors: shown to the person, never an automatic redirect ----


def assert_error_page(response: httpx.Response, error: str) -> dict[str, str]:
    """A request error page that names the return host and carries the error in its link."""
    assert response.status_code == 400
    assert "location" not in response.headers
    assert "This connection request has a problem" in response.text
    assert '<strong class="host">assistant.example.test</strong>' in response.text
    assert "Return to assistant.example.test" in response.text
    query = link_query(response.text)
    assert query["error"] == error
    assert query["iss"] == BASE_URL
    assert "code" not in query
    return query


async def test_a_request_error_shows_a_page_with_a_return_link_and_never_redirects(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser)
    response = await browser.get(
        "/oauth/authorize", params=authorize_params(client_id, response_type="token")
    )
    query = assert_error_page(response, "unsupported_response_type")
    assert query["state"] == "state-123"
    assert re.search(rf'href="{re.escape(CALLBACK)}\?', response.text)


async def test_the_server_cannot_be_used_as_an_open_redirector(
    browser: httpx.AsyncClient,
) -> None:
    """Any bad request, in any parameter, only ever yields a page: no Location header."""
    client_id = await client_id_of(browser)
    bad = [
        {"response_type": "token"},
        {"response_type": None},
        {"code_challenge": None},
        {"code_challenge_method": "plain"},
        {"resource": "https://other.example/mcp"},
        {"state": "s" * 600},
    ]
    for overrides in bad:
        response = await browser.get(
            "/oauth/authorize", params=authorize_params(client_id, **overrides)
        )
        assert response.status_code == 400, overrides
        assert "location" not in response.headers, overrides


async def test_a_signed_out_visitor_with_a_bad_request_goes_to_sign_in_first(
    anonymous: httpx.AsyncClient, person: int
) -> None:
    client_id = await client_id_of(anonymous)
    params = authorize_params(client_id, code_challenge=None)
    response = await anonymous.get("/oauth/authorize", params=params)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert (
        CALLBACK
        not in response.headers["location"]
        .replace("%3A", ":")
        .replace("%2F", "/")
        .split("next=")[0]
    )
    # After signing in the person sees the error page, still without a redirect.
    await sign_in(anonymous)
    after = await anonymous.get("/oauth/authorize", params=params)
    assert_error_page(after, "invalid_request")


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_challenge": None},
        {"code_challenge": "short"},
        {"code_challenge": "!" * 43},
        {"code_challenge": "a" * 44},
        {"code_challenge": "a" * 42 + "~"},
        {"code_challenge": "a" * 42 + "."},
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"code_challenge_method": "s256"},
    ],
)
async def test_pkce_with_s256_is_required(
    browser: httpx.AsyncClient, overrides: dict[str, str | None]
) -> None:
    client_id = await client_id_of(browser)
    response = await browser.get(
        "/oauth/authorize", params=authorize_params(client_id, **overrides)
    )
    assert_error_page(response, "invalid_request")


@pytest.mark.parametrize(
    "resource",
    [
        "https://other.example/mcp",
        BASE_URL,
        f"{BASE_URL}/other",
        f"{BASE_URL}/mcp?x=1",
        f"{BASE_URL}/mcp#frag",
        "http://pms.example.test/mcp",
    ],
)
async def test_a_resource_other_than_the_mcp_endpoint_is_refused(
    browser: httpx.AsyncClient, resource: str
) -> None:
    client_id = await client_id_of(browser)
    response = await browser.get(
        "/oauth/authorize", params=authorize_params(client_id, resource=resource)
    )
    assert_error_page(response, "invalid_target")


@pytest.mark.parametrize(
    "resource", [None, f"{BASE_URL}/mcp", f"{BASE_URL}/mcp/", "HTTPS://PMS.example.test/mcp"]
)
async def test_the_mcp_resource_is_accepted_or_defaulted(
    browser: httpx.AsyncClient, resource: str | None
) -> None:
    client_id = await client_id_of(browser)
    params = authorize_params(client_id, resource=resource)
    response = await approve(browser, params)
    assert response.status_code == 303
    assert "code" in location_query(response)
    async with session() as db:
        grant = (await db.execute(select(OAuthGrant))).scalar_one()
    assert grant.resource == f"{BASE_URL}/mcp"


async def test_a_repeated_parameter_is_refused(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in authorize_params(client_id).items())
    response = await browser.get(f"/oauth/authorize?{query}&state=other")
    assert_error_page(response, "invalid_request")


async def test_an_overlong_state_is_refused_and_not_echoed(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    response = await browser.get(
        "/oauth/authorize", params=authorize_params(client_id, state="s" * 600)
    )
    query = assert_error_page(response, "invalid_request")
    assert "state" not in query
    assert "s" * 100 not in response.text


# ---- Signing in and coming back ----


async def test_a_signed_out_visitor_signs_in_and_returns_to_the_consent_page(
    anonymous: httpx.AsyncClient, person: int
) -> None:
    client_id = await client_id_of(anonymous)
    params = authorize_params(client_id, state="a b&c=d")
    first = await anonymous.get("/oauth/authorize", params=params)
    assert first.status_code == 303
    target = first.headers["location"]
    assert target.startswith("/login?next=")
    page = await anonymous.get(target)
    assert page.status_code == 200
    next_field = re.search(r'name="next" value="([^"]*)"', page.text)
    assert next_field
    response = await anonymous.post(
        "/login",
        data={
            "email": EMAIL,
            "password": PASSWORD,
            "csrf_token": page_token(page.text),
            "next": html.unescape(next_field.group(1)),
        },
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/oauth/authorize?")
    consent_page = await anonymous.get(response.headers["location"])
    assert consent_page.status_code == 200
    assert "Example Assistant" in consent_page.text
    approved = await approve(anonymous, params)
    assert location_query(approved)["state"] == "a b&c=d"


# ---- The consent page ----


async def test_the_consent_page_shows_the_client_and_where_it_returns(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser, client_name="Example Assistant")
    page = await browser.get("/oauth/authorize", params=authorize_params(client_id))
    assert page.status_code == 200
    assert "Example Assistant" in page.text
    assert '<strong class="host">assistant.example.test</strong>' in page.text
    assert "You have no PMS connections yet" in page.text
    assert "once they exist" in page.text
    assert "on the computer you are using" not in page.text
    assert page.headers["cache-control"] == "no-store, no-transform"


async def test_the_client_name_is_escaped(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser, client_name="<script>alert(1)</script> & co")
    page = await browser.get("/oauth/authorize", params=authorize_params(client_id))
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in page.text


async def test_the_redirect_host_cannot_be_disguised_by_userinfo_tricks(
    browser: httpx.AsyncClient,
) -> None:
    response = await browser.post(
        "/oauth/register",
        json={
            "client_name": "X",
            "redirect_uris": ["https://assistant.example.test@evil.example/cb"],
        },
    )
    assert response.status_code == 400


async def test_the_consent_policy_adds_only_the_redirect_origin_to_form_action(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser)
    page = await browser.get("/oauth/authorize", params=authorize_params(client_id))
    expected = PAGE_CSP.replace(
        "form-action 'self'", "form-action 'self' https://assistant.example.test"
    )
    assert page.headers["content-security-policy"] == expected
    directives = dict(part.strip().split(" ", 1) for part in expected.split(";"))
    assert directives["form-action"] == "'self' https://assistant.example.test"
    assert directives["default-src"] == "'none'"
    assert "script-src" not in directives


async def test_the_consent_policy_for_a_loopback_redirect_names_its_origin_and_warns(
    browser: httpx.AsyncClient,
) -> None:
    uri = "http://127.0.0.1:53682/callback"
    client_id = await client_id_of(browser, redirect_uris=[uri])
    page = await browser.get(
        "/oauth/authorize", params=authorize_params(client_id, redirect_uri=uri)
    )
    assert "form-action 'self' http://127.0.0.1:53682;" in page.headers["content-security-policy"]
    assert "on the computer you are using" in page.text
    assert '<strong class="host">127.0.0.1</strong>' in page.text


async def test_the_loopback_port_may_differ_and_the_code_goes_to_the_requested_port(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser, redirect_uris=["http://127.0.0.1:5000/callback"])
    uri = "http://127.0.0.1:61000/callback"
    params = authorize_params(client_id, redirect_uri=uri)
    page = await browser.get("/oauth/authorize", params=params)
    assert "form-action 'self' http://127.0.0.1:61000;" in page.headers["content-security-policy"]
    response = await approve(browser, params)
    assert response.headers["location"].startswith(uri + "?")


# ---- Approving and cancelling ----


async def test_approving_creates_a_grant_and_a_single_use_code(
    browser: httpx.AsyncClient, person: int, caplog: pytest.LogCaptureFixture
) -> None:
    client_id = await client_id_of(browser)
    params = authorize_params(client_id)
    with caplog.at_level(logging.INFO, logger="pmsbridge.oauth"):
        response = await approve(browser, params)
    assert response.status_code == 303
    assert response.headers["location"].startswith(CALLBACK + "?")
    query = location_query(response)
    assert query["state"] == "state-123"
    assert query["iss"] == BASE_URL
    code = query["code"]
    assert len(code) >= 43
    async with session() as db:
        grant = (await db.execute(select(OAuthGrant))).scalar_one()
        stored = (await db.execute(select(OAuthCode))).scalar_one()
        client = await db.get(OAuthClient, client_id)
    assert (grant.user_id, grant.client_id) == (person, client_id)
    # No connections exist yet, so the approval covers none (not "all, including later ones").
    assert grant.all_connections is False
    assert grant.allowed_connection_ids == []
    assert grant.scope == "mcp"
    assert grant.revoked_at is None
    assert stored.code_hash == hash_token(code)
    assert code not in stored.code_hash
    assert stored.code_challenge == challenge_for()
    assert stored.redirect_uri == CALLBACK
    assert stored.resource == f"{BASE_URL}/mcp"
    assert (
        timedelta(minutes=4, seconds=50)
        < stored.expires_at - stored.created_at
        <= timedelta(minutes=5)
    )
    assert client is not None and client.last_used_at is not None
    authorized = [r for r in caplog.records if r.getMessage() == "assistant authorized"]
    assert len(authorized) == 1
    assert (authorized[0].user_id, authorized[0].client_id) == (person, client_id)  # pyright: ignore[reportAttributeAccessIssue]
    assert code not in caplog.text


async def test_each_approval_gets_its_own_code(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    first = location_query(await approve(browser, authorize_params(client_id)))["code"]
    second = location_query(await approve(browser, authorize_params(client_id)))["code"]
    assert first != second


async def test_cancelling_redirects_with_access_denied_and_creates_nothing(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser)
    response = await approve(browser, authorize_params(client_id), decision="deny")
    assert response.status_code == 303
    query = location_query(response)
    assert query["error"] == "access_denied"
    assert query["state"] == "state-123"
    assert query["iss"] == BASE_URL
    assert "code" not in query
    async with session() as db:
        assert (await db.execute(select(OAuthGrant))).first() is None
        assert (await db.execute(select(OAuthCode))).first() is None


async def test_a_state_without_a_value_is_left_out(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    response = await approve(browser, authorize_params(client_id, state=None))
    assert "state" not in location_query(response)


# ---- The consent form is checked again on submission ----


async def post_consent(
    client: httpx.AsyncClient, params: dict[str, str], *, token: str | None = None, **extra: str
) -> httpx.Response:
    if token is None:
        token = page_token((await client.get("/oauth/authorize", params=params)).text)
    data = {**params, "decision": "approve", **extra}
    if token:
        data["csrf_token"] = token
    return await client.post("/oauth/authorize", data=data, headers={"Origin": BASE_URL})


async def test_the_consent_form_needs_the_csrf_token(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    response = await post_consent(browser, authorize_params(client_id), token="")
    assert response.status_code == 403
    assert "location" not in response.headers
    async with session() as db:
        assert (await db.execute(select(OAuthCode))).first() is None


async def test_the_consent_form_needs_the_site_origin(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    params = authorize_params(client_id)
    token = page_token((await browser.get("/oauth/authorize", params=params)).text)
    for headers in ({"Origin": "https://evil.example"}, {}):
        response = await browser.post(
            "/oauth/authorize",
            data={**params, "decision": "approve", "csrf_token": token},
            headers=headers,
        )
        assert response.status_code == 403
        assert "location" not in response.headers


async def test_a_signed_out_post_creates_nothing(anonymous: httpx.AsyncClient, person: int) -> None:
    client_id = await client_id_of(anonymous)
    response = await anonymous.post(
        "/oauth/authorize",
        data={**authorize_params(client_id), "decision": "approve"},
        headers={"Origin": BASE_URL},
    )
    assert response.status_code == 401
    async with session() as db:
        assert (await db.execute(select(OAuthCode))).first() is None


async def test_the_form_cannot_be_used_to_redirect_to_another_address(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser)
    params = authorize_params(client_id)
    token = page_token((await browser.get("/oauth/authorize", params=params)).text)
    for decision in ("approve", "deny"):
        response = await browser.post(
            "/oauth/authorize",
            data={
                **params,
                "redirect_uri": "https://evil.example/steal",
                "decision": decision,
                "csrf_token": token,
            },
            headers={"Origin": BASE_URL},
        )
        assert response.status_code == 400
        assert "location" not in response.headers
    async with session() as db:
        assert (await db.execute(select(OAuthCode))).first() is None


async def test_the_form_is_validated_like_the_request(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    params = authorize_params(client_id)
    token = page_token((await browser.get("/oauth/authorize", params=params)).text)
    response = await browser.post(
        "/oauth/authorize",
        data={
            **params,
            "code_challenge_method": "plain",
            "decision": "approve",
            "csrf_token": token,
        },
        headers={"Origin": BASE_URL},
    )
    assert_error_page(response, "invalid_request")


async def test_cancelling_a_request_with_a_bad_parameter_still_shows_a_page(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser)
    params = authorize_params(client_id)
    token = page_token((await browser.get("/oauth/authorize", params=params)).text)
    response = await browser.post(
        "/oauth/authorize",
        data={
            **params,
            "response_type": "token",
            "decision": "deny",
            "csrf_token": token,
        },
        headers={"Origin": BASE_URL},
    )
    assert_error_page(response, "unsupported_response_type")


async def test_a_form_without_a_decision_is_refused(browser: httpx.AsyncClient) -> None:
    client_id = await client_id_of(browser)
    params = authorize_params(client_id)
    response = await post_consent(browser, params, decision="maybe")
    assert response.status_code == 400
    assert "location" not in response.headers


# ---- Choosing connections (the list is filled by the PMS feature) ----


def install_connections(*labels: tuple[str, str]) -> None:
    choices = [consent.ConnectionChoice(id=i, label=label) for i, label in labels]

    async def source(_user_id: int) -> Sequence[consent.ConnectionChoice]:
        return choices

    consent.set_choice_source(source)


async def test_the_consent_page_lists_the_connections_to_choose_from(
    browser: httpx.AsyncClient,
) -> None:
    install_connections(("c1", "Beach houses (Guesty)"), ("c2", "City flats (Hostaway)"))
    client_id = await client_id_of(browser)
    page = await browser.get("/oauth/authorize", params=authorize_params(client_id))
    assert "Beach houses (Guesty)" in page.text
    assert 'name="connection" value="c2"' in page.text
    assert "no PMS connections yet" not in page.text


async def test_a_selection_of_connections_is_stored(browser: httpx.AsyncClient) -> None:
    install_connections(("c1", "One"), ("c2", "Two"), ("c3", "Three"))
    client_id = await client_id_of(browser)
    response = await post_consent(
        browser, authorize_params(client_id), mode="selected", connection="c1"
    )
    assert response.status_code == 303
    async with session() as db:
        grant = (await db.execute(select(OAuthGrant))).scalar_one()
    assert grant.all_connections is False
    assert grant.allowed_connection_ids == ["c1"]


async def test_choosing_all_connections_is_stored_as_all(browser: httpx.AsyncClient) -> None:
    install_connections(("c1", "One"), ("c2", "Two"))
    client_id = await client_id_of(browser)
    response = await post_consent(browser, authorize_params(client_id), mode="all")
    assert response.status_code == 303
    async with session() as db:
        grant = (await db.execute(select(OAuthGrant))).scalar_one()
    assert grant.all_connections is True and grant.allowed_connection_ids == []


@pytest.mark.parametrize(
    "extra",
    [{}, {"mode": "selected"}, {"mode": "selected", "connection": "c9"}, {"mode": "other"}],
)
async def test_an_empty_or_foreign_selection_is_refused_and_creates_nothing(
    browser: httpx.AsyncClient, extra: dict[str, str]
) -> None:
    install_connections(("c1", "One"), ("c2", "Two"))
    client_id = await client_id_of(browser)
    response = await post_consent(browser, authorize_params(client_id), **extra)
    assert response.status_code == 400
    assert "Choose which connections" in response.text
    assert "location" not in response.headers
    async with session() as db:
        assert (await db.execute(select(OAuthCode))).first() is None


async def test_the_redirect_target_has_no_userinfo_or_extra_host(
    browser: httpx.AsyncClient,
) -> None:
    client_id = await client_id_of(browser)
    response = await approve(browser, authorize_params(client_id))
    parts = urlsplit(response.headers["location"])
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "assistant.example.test",
        "/callback",
    )
