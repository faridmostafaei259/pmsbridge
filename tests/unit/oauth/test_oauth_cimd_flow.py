"""Clients named by a web address: documents, caching, limits and the whole flow."""
# ruff: noqa: S106  (fake client metadata, not passwords)

import asyncio
import html
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cimd_helpers import CALLBACK as DOC_CALLBACK
from cimd_helpers import CLIENT_URL, FakeInternet, document, http_response, json_response
from fastapi import FastAPI
from oauth_helpers import (
    Browser,
    approve,
    authorize_params,
    code_form,
    get_code,
    register_client,
    token_request,
)
from sqlalchemy import select, update

from pmsbridge.core.storage import session, write_transaction
from pmsbridge.oauth import clients
from pmsbridge.oauth.client_metadata import (
    CACHE_DEFAULT,
    CACHE_MAX,
    CACHE_MIN,
    ClientMetadataService,
    FetchLimiter,
    FetchRateLimitedError,
    clamp_seconds,
    derived_client_id,
    parse_metadata_document,
    set_metadata_service,
)
from pmsbridge.oauth.db import OAuthClient
from pmsbridge.oauth.metadata_fetch import MetadataFetchError, validate_client_id_url
from pmsbridge.oauth.tokens import validate_access_token

HOST = "assistant.example.test"
PERSON = 1
OTHER_URL = "https://assistant.example.test/x.json"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(app: FastAPI, internet: FakeInternet, clock: Clock) -> ClientMetadataService:
    built = ClientMetadataService(internet.fetcher(), clock=clock)
    set_metadata_service(built)
    return built


def other_url(n: int, host: str = HOST) -> str:
    return f"https://{host}/client-{n}.json"


# ---- The document ----


def parse(body: object) -> object:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return parse_metadata_document(validate_client_id_url(CLIENT_URL), raw)


def test_a_good_document_is_read_and_unknown_fields_are_ignored() -> None:
    data = parse(document())
    assert data.name == "Example Assistant"  # type: ignore[attr-defined]
    assert data.redirect_uris == [DOC_CALLBACK]  # type: ignore[attr-defined]
    assert data.auth_method == "none"  # type: ignore[attr-defined]
    assert data.grant_types == ["authorization_code", "refresh_token"]  # type: ignore[attr-defined]


def test_only_the_required_fields_are_needed() -> None:
    data = parse({"client_id": CLIENT_URL, "client_name": "A", "redirect_uris": [DOC_CALLBACK]})
    assert data.grant_types == ["authorization_code"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (document(client_id="https://assistant.example.test/other.json"), "does not match"),
        (document(client_id=None), "does not match"),
        (document(client_id=CLIENT_URL + "/"), "does not match"),
        (document(client_id=CLIENT_URL.upper()), "does not match"),
        (document(client_secret="fake-secret"), "client secret"),
        (document(client_secret_expires_at=0), "client secret"),
        (document(token_endpoint_auth_method="client_secret_post"), "not supported"),
        (document(token_endpoint_auth_method="client_secret_basic"), "not supported"),
        (
            document(token_endpoint_auth_method="private_key_jwt", jwks_uri="https://x.test/j"),
            "not supported",
        ),
        (document(token_endpoint_auth_method=["none"]), "not supported"),
        (document(client_name=None), "client_name"),
        (document(client_name=""), "client_name"),
        (document(client_name="x" * 101), "client_name"),
        (document(client_name="bad\x00name"), "not allowed"),
        (document(client_name=5), "client_name"),
        (document(redirect_uris=None), "redirect_uris"),
        (document(redirect_uris=[]), "redirect_uris"),
        (document(redirect_uris=["http://assistant.example.test/cb"]), "https"),
        (document(redirect_uris=["https://assistant.example.test/cb#frag"]), "fragment"),
        (document(redirect_uris=["javascript:alert(1)"]), "https"),
        (document(redirect_uris=["https://a.test/cb"] * 11 + ["https://b.test/cb"]), "At most"),
        (document(grant_types=["password"]), "grant_types"),
        (document(grant_types=["refresh_token"]), "authorization_code"),
        (document(response_types=["token"]), "response_types"),
        (["not", "an", "object"], "JSON object"),
        ("a string", "JSON object"),
        (b"{not json", "not valid JSON"),
        (b"[" * 100000, "not valid JSON"),
        (b"\xff\xfe\x00", "not valid JSON"),
    ],
)
def test_a_bad_document_is_refused_with_a_fixed_message(body: object, message: str) -> None:
    with pytest.raises(MetadataFetchError, match=message) as caught:
        parse(body)
    assert "fake-secret" not in str(caught.value)


def test_loopback_redirects_are_allowed_as_for_registered_clients() -> None:
    data = parse(document(redirect_uris=["http://127.0.0.1:3000/callback", "http://localhost/cb"]))
    assert len(data.redirect_uris) == 2  # type: ignore[attr-defined]


# ---- Cache lifetime ----


@pytest.mark.parametrize(
    ("max_age", "expected"),
    [
        (None, CACHE_DEFAULT),
        (0, CACHE_MIN),
        (60, CACHE_MIN),
        (300, 300),
        (4000, 4000),
        (86400, CACHE_MAX),
        (10**9, CACHE_MAX),
    ],
)
def test_the_cache_lifetime_is_clamped_between_five_minutes_and_a_day(
    max_age: int | None, expected: int
) -> None:
    assert clamp_seconds(max_age) == expected
    assert (CACHE_MIN, CACHE_MAX) == (300, 86400)


async def stored(url: str = CLIENT_URL) -> OAuthClient:
    async with session() as db:
        return (
            await db.execute(select(OAuthClient).where(OAuthClient.client_url == url))
        ).scalar_one()


async def test_a_fetched_client_is_stored_as_an_ordinary_public_client(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    internet.serve_document()
    client = await service.resolve(CLIENT_URL, PERSON)
    assert client.client_id == derived_client_id(CLIENT_URL)
    assert len(client.client_id) == 48
    row = await stored()
    assert (row.kind, row.token_endpoint_auth_method, row.client_secret_hash) == (
        "metadata_document",
        "none",
        None,
    )
    assert row.public_id == CLIENT_URL
    assert row.fetched_at is not None and row.cache_until is not None
    assert row.cache_until - row.fetched_at == timedelta(seconds=CACHE_DEFAULT)


async def test_the_server_cache_header_sets_the_stored_lifetime(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    for header, seconds in (
        ("max-age=60", CACHE_MIN),
        ("max-age=7200", 7200),
        ("max-age=99999999", CACHE_MAX),
    ):
        url = other_url(seconds)
        internet.serve(HOST, json_response(document(url), headers={"Cache-Control": header}))
        await service.resolve(url, PERSON)
        row = await stored(url)
        assert row.cache_until - row.fetched_at == timedelta(seconds=seconds)  # type: ignore[operator]


async def test_a_fresh_copy_is_used_without_another_fetch(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    internet.serve_document()
    await service.resolve(CLIENT_URL, PERSON)
    await service.resolve(CLIENT_URL, PERSON)
    assert internet.connects_to(HOST) == 1


async def test_a_stale_copy_is_fetched_again_and_updated(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    internet.serve_document()
    first = await service.resolve(CLIENT_URL, PERSON)
    async with write_transaction() as db:
        await db.execute(
            update(OAuthClient).values(cache_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    internet.serve_document(client_name="Renamed", redirect_uris=["https://new.example.test/cb"])
    second = await service.resolve(CLIENT_URL, PERSON)
    assert internet.connects_to(HOST) == 2
    assert second.client_id == first.client_id  # same client, so its approvals stay
    assert (second.client_name, second.redirect_uris) == (
        "Renamed",
        ["https://new.example.test/cb"],
    )
    assert (await stored()).client_name == "Renamed"


async def test_failures_are_remembered_for_five_minutes_then_fetched_again(
    service: ClientMetadataService, internet: FakeInternet, clock: Clock
) -> None:
    internet.serve(HOST, http_response(b"{}", status=500))
    for _ in range(3):
        with pytest.raises(MetadataFetchError, match="error"):
            await service.resolve(CLIENT_URL, PERSON)
    assert internet.connects_to(HOST) == 1
    clock.now += 299
    with pytest.raises(MetadataFetchError):
        await service.resolve(CLIENT_URL, PERSON)
    assert internet.connects_to(HOST) == 1
    clock.now += 2
    internet.serve_document()
    assert (await service.resolve(CLIENT_URL, PERSON)).client_url == CLIENT_URL
    assert internet.connects_to(HOST) == 2


async def test_an_invalid_document_is_not_stored_and_is_remembered_as_a_failure(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    internet.serve_document(client_id="https://assistant.example.test/other.json")
    for _ in range(2):
        with pytest.raises(MetadataFetchError, match="does not match"):
            await service.resolve(CLIENT_URL, PERSON)
    assert internet.connects_to(HOST) == 1
    async with session() as db:
        assert (await db.execute(select(OAuthClient))).first() is None


async def test_an_address_that_resolves_privately_is_refused_and_remembered(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    internet.serve(HOST, json_response(document()), address="10.1.2.3")
    for _ in range(2):
        with pytest.raises(MetadataFetchError, match="does not contact"):
            await service.resolve(CLIENT_URL, PERSON)
    assert internet.connects == []
    assert internet.lookups == [HOST]


# ---- Rate limits ----


def test_the_limiter_allows_five_per_host_ten_per_person_and_thirty_in_all_each_hour() -> None:
    clock = Clock()
    limiter = FetchLimiter(clock=clock)
    assert [limiter.admit("a.test", 1) for _ in range(6)] == [None] * 5 + ["host"]
    assert limiter.admit("b.test", 1) is None  # another host is unaffected
    clock.now += 3599
    assert limiter.admit("a.test", 1) == "host"
    clock.now += 2
    assert limiter.admit("a.test", 1) is None
    person = FetchLimiter(clock=clock)
    results = [person.admit(f"h{n}.test", 7) for n in range(11)]
    assert results == [None] * 10 + ["person"]
    assert person.admit("other.test", 8) is None  # another person is unaffected
    clock.now += 3601
    assert person.admit("h0.test", 7) is None
    fresh = FetchLimiter(clock=clock)
    results = [fresh.admit(f"h{n}.test", n) for n in range(31)]
    assert results == [None] * 30 + ["global"]
    clock.now += 3601
    assert fresh.admit("h0.test", 0) is None


async def test_a_refused_fetch_does_not_use_up_any_allowance() -> None:
    limiter = FetchLimiter(per_host=1)
    assert limiter.admit("a.test", 1) is None
    assert [limiter.admit("a.test", 1) for _ in range(5)] == ["host"] * 5
    assert limiter.admit("b.test", 1) is None


async def test_the_eleventh_fetch_by_one_person_is_refused_and_logged_as_a_warning(
    service: ClientMetadataService,
    internet: FakeInternet,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for n in range(11):
        internet.serve(f"h{n}.example.test", http_response(b"{}", status=404))
    caplog.set_level(logging.INFO)
    for n in range(10):
        with pytest.raises(MetadataFetchError, match="error"):
            await service.resolve(f"https://h{n}.example.test/c.json", PERSON)
    with pytest.raises(MetadataFetchError, match="Too many"):
        await service.resolve("https://h10.example.test/c.json", PERSON)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [(r.metadata_host, r.limit) for r in warnings] == [("h10.example.test", "person")]  # pyright: ignore[reportAttributeAccessIssue]
    assert "/c.json" not in caplog.text


async def test_the_sixth_fetch_for_one_host_in_an_hour_is_refused_before_any_request(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    internet.serve(HOST, http_response(b"{}", status=404))
    for n in range(5):
        with pytest.raises(MetadataFetchError, match="error"):
            await service.resolve(other_url(n), PERSON)
    assert internet.connects_to(HOST) == 5
    with pytest.raises(MetadataFetchError, match="Too many") as caught:
        await service.resolve(other_url(99), PERSON)
    assert type(caught.value).__name__ == "FetchRateLimitedError"
    assert internet.connects_to(HOST) == 5
    # Not remembered as a failure of that address: it works once the hour has passed.


async def test_requests_served_from_the_cache_do_not_count_against_the_limits(
    service: ClientMetadataService, internet: FakeInternet
) -> None:
    internet.serve_document()
    for _ in range(20):
        await service.resolve(CLIENT_URL, PERSON)
    assert internet.connects_to(HOST) == 1


# ---- The authorization flow ----


@pytest.fixture
def web(internet: FakeInternet, service: ClientMetadataService) -> FakeInternet:
    internet.serve_document()
    return internet


async def test_the_server_metadata_advertises_support(anonymous: httpx.AsyncClient) -> None:
    body = (await anonymous.get("/.well-known/oauth-authorization-server")).json()
    assert body["client_id_metadata_document_supported"] is True
    assert "registration_endpoint" in body  # registration stays available


async def test_the_consent_page_shows_the_verified_host_the_name_and_the_return_host(
    browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    web.serve_document(client_name="<b>Fancy</b> Assistant")
    page = await browser.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert page.status_code == 200
    assert "This assistant is identified by" in page.text
    assert f'<strong class="host">{HOST}</strong>' in page.text
    assert html.escape(CLIENT_URL) in page.text
    assert "&lt;b&gt;Fancy&lt;/b&gt; Assistant" in page.text
    assert "<b>Fancy</b>" not in page.text
    assert "your browser goes back to" in page.text
    assert 'name="client_id" value="https://assistant.example.test/client.json"' in page.text


async def test_a_registered_client_is_not_shown_as_identified_by_an_address(
    browser: httpx.AsyncClient, anonymous: httpx.AsyncClient
) -> None:
    client_id = str((await register_client(anonymous))["client_id"])
    page = await browser.get("/oauth/authorize", params=authorize_params(client_id))
    assert page.status_code == 200
    assert "identified by" not in page.text


async def test_a_web_address_client_goes_from_authorization_to_tokens_and_refresh(
    browser: httpx.AsyncClient, anonymous: httpx.AsyncClient, web: FakeInternet, person: int
) -> None:
    code = await get_code(browser, CLIENT_URL)
    assert web.connects_to(HOST) == 1  # one fetch served the page and the approval
    response = await token_request(anonymous, code_form(CLIENT_URL, code))
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert tokens["refresh_token"]
    principal = await validate_access_token(tokens["access_token"])
    assert principal is not None and principal.user_id == person
    assert principal.client_id == derived_client_id(CLIENT_URL)
    refreshed = await token_request(
        anonymous,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": CLIENT_URL,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert web.connects_to(HOST) == 1  # the token endpoint never fetched anything


async def test_a_visitor_who_is_not_signed_in_causes_no_fetch_and_is_sent_to_sign_in(
    make_browser: Browser, browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    visitor = make_browser("203.0.113.9")
    fresh = await visitor.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert fresh.status_code == 303 and fresh.headers["location"].startswith("/login")
    assert web.lookups == [] and web.connects == []  # nothing before sign-in, redirect not judged
    redirect = await visitor.get(
        "/oauth/authorize",
        params=authorize_params(CLIENT_URL, redirect_uri="https://evil.example.test/cb"),
    )
    assert redirect.status_code == 303 and web.lookups == []
    page = await browser.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert page.status_code == 200
    assert web.connects_to(HOST) == 1  # the fetch happened after sign-in, for the person
    again = await browser.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert again.status_code == 200 and web.connects_to(HOST) == 1


async def test_before_sign_in_a_stale_stored_copy_judges_the_redirect_uri_without_a_fetch(
    make_browser: Browser, browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    assert (
        await browser.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    ).status_code == 200
    async with write_transaction() as db:
        await db.execute(
            update(OAuthClient).values(cache_until=datetime.now(UTC) - timedelta(hours=1))
        )
    visitor = make_browser("203.0.113.9")
    bad = await visitor.get(
        "/oauth/authorize",
        params=authorize_params(CLIENT_URL, redirect_uri="https://evil.example.test/cb"),
    )
    assert bad.status_code == 400 and "location" not in bad.headers
    good = await visitor.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert good.status_code == 303 and good.headers["location"].startswith("/login")
    assert web.connects_to(HOST) == 1  # the stale copy was not refreshed for a visitor
    signed_in = await browser.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert signed_in.status_code == 200 and web.connects_to(HOST) == 2


async def test_a_badly_formed_address_is_an_error_page_even_before_sign_in(
    make_browser: Browser, web: FakeInternet
) -> None:
    visitor = make_browser("203.0.113.9")
    response = await visitor.get(
        "/oauth/authorize", params=authorize_params("https://assistant.example.test:8443/c.json")
    )
    assert response.status_code == 400 and "location" not in response.headers
    assert web.lookups == []


async def test_a_redirect_uri_outside_the_document_is_an_error_page(
    browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    params = authorize_params(CLIENT_URL, redirect_uri="https://evil.example.test/cb")
    response = await browser.get("/oauth/authorize", params=params)
    assert response.status_code == 400
    assert "location" not in response.headers
    assert "did not register" in response.text


@pytest.mark.parametrize(
    ("reply", "text"),
    [
        (http_response(b"{}", status=302, headers={"Location": "https://10.0.0.1/"}), "redirects"),
        (http_response(b"{}", status=500), "error"),
        (http_response(b"<html>", headers={"Content-Type": "text/html"}), "did not return JSON"),
        (
            json_response(document(client_id="https://assistant.example.test/x.json")),
            "does not match",
        ),
        (json_response(document(token_endpoint_auth_method="client_secret_post")), "not supported"),
        (http_response(b"not json"), "not valid JSON"),
    ],
)
async def test_every_fetch_or_document_failure_shows_an_error_page_never_a_redirect(
    browser: httpx.AsyncClient, web: FakeInternet, reply: bytes, text: str
) -> None:
    web.serve(HOST, reply)
    response = await browser.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert response.status_code == 400
    assert "location" not in response.headers
    assert text in response.text
    assert CLIENT_URL not in response.text  # the address is not echoed back


async def test_bad_addresses_and_private_hosts_are_error_pages_without_any_lookup(
    browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    for bad in (
        "http://assistant.example.test/c.json",
        "https://10.0.0.1/c.json",
        "https://assistant.example.test:8443/c.json",
    ):
        response = await browser.get("/oauth/authorize", params=authorize_params(bad))
        assert response.status_code == 400 and "location" not in response.headers
    assert web.lookups == [] and web.connects == []


async def test_the_fetch_limit_shows_a_429_page(
    browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    web.serve(HOST, http_response(b"{}", status=404))
    statuses = [
        (await browser.get("/oauth/authorize", params=authorize_params(other_url(n)))).status_code
        for n in range(6)
    ]
    assert statuses == [400] * 5 + [429]


async def test_a_repeated_client_id_parameter_fetches_nothing(
    browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    response = await browser.get(
        "/oauth/authorize",
        params=[
            *authorize_params(CLIENT_URL).items(),
            ("client_id", "https://other.example.test/c"),
        ],
    )
    assert response.status_code == 400
    assert web.connects == []


async def test_the_token_endpoint_never_fetches_and_the_short_key_is_not_an_id(
    anonymous: httpx.AsyncClient, browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    unknown = "https://never-seen.example.test/c.json"
    web.serve("never-seen.example.test", json_response(document(unknown)))
    response = await token_request(anonymous, code_form(unknown, "code"))
    assert response.status_code == 400 and response.json()["error"] == "invalid_client"
    assert web.lookups == []
    await get_code(browser, CLIENT_URL)
    key = derived_client_id(CLIENT_URL)
    assert await clients.get_client(key) is None
    assert (await clients.get_client(CLIENT_URL)) is not None
    page = await browser.get("/oauth/authorize", params=authorize_params(key))
    assert page.status_code == 400 and "not registered" in page.text
    response = await token_request(anonymous, code_form(key, "code"))
    assert response.json()["error"] == "invalid_client"


async def test_the_approval_lists_show_the_verified_host(
    browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    await get_code(browser, CLIENT_URL)
    mine = await browser.get("/account/apps")
    assert "Identified by" in mine.text and HOST in mine.text


async def test_denying_still_returns_to_the_documents_redirect_address(
    browser: httpx.AsyncClient, web: FakeInternet
) -> None:
    response = await approve(browser, authorize_params(CLIENT_URL), decision="deny")
    assert response.status_code == 303
    assert response.headers["location"].startswith(DOC_CALLBACK + "?error=access_denied")


async def test_an_expired_unused_client_is_fetched_again(
    service: ClientMetadataService, web: FakeInternet
) -> None:
    await service.resolve(CLIENT_URL, PERSON)
    async with write_transaction() as db:
        await db.execute(
            update(OAuthClient).values(created_at=datetime.now(UTC) - timedelta(days=2))
        )
    assert await clients.get_client(CLIENT_URL) is None
    await service.resolve(CLIENT_URL, PERSON)
    assert web.connects_to(HOST) == 2
    assert await clients.get_client(CLIENT_URL) is not None


# ---- One fetch at a time ----


async def test_two_concurrent_lookups_of_one_address_make_one_fetch(
    app: FastAPI, internet: FakeInternet
) -> None:
    internet.serve_document()
    internet.delay = 0.05
    service = ClientMetadataService(internet.fetcher())
    first, second = await asyncio.gather(
        service.resolve(CLIENT_URL, 1), service.resolve(CLIENT_URL, 2)
    )
    assert first.client_id == second.client_id
    assert internet.connects_to(HOST) == 1


async def test_a_hanging_fetch_does_not_delay_another_caller_beyond_the_wait_limit(
    app: FastAPI, internet: FakeInternet
) -> None:
    internet.serve_document()
    internet.serve("slow.example.test", http_response(b"{}"))
    internet.delay = 3.0
    service = ClientMetadataService(internet.fetcher(total_timeout=1.5), lock_wait=0.2)
    hanging = asyncio.create_task(service.resolve("https://slow.example.test/c.json", 1))
    await asyncio.sleep(0.1)  # the first caller now holds the turn
    started = time.monotonic()
    with pytest.raises(FetchRateLimitedError, match="Too many"):
        await service.resolve(CLIENT_URL, 2)
    assert time.monotonic() - started < 1.0
    with pytest.raises(MetadataFetchError, match="did not answer in time"):
        await hanging
    assert internet.connects_to(HOST) == 0
    internet.delay = 0.0
    assert (await service.resolve(CLIENT_URL, 2)).client_url == CLIENT_URL  # the turn was released


async def test_the_authorization_page_shows_429_when_the_turn_is_not_available(
    browser: httpx.AsyncClient, internet: FakeInternet
) -> None:
    internet.serve_document()
    internet.serve("slow.example.test", http_response(b"{}"))
    internet.delay = 3.0
    service = ClientMetadataService(internet.fetcher(total_timeout=1.5), lock_wait=0.2)
    set_metadata_service(service)
    hanging = asyncio.create_task(service.resolve("https://slow.example.test/c.json", 1))
    await asyncio.sleep(0.1)
    response = await browser.get("/oauth/authorize", params=authorize_params(CLIENT_URL))
    assert response.status_code == 429 and "location" not in response.headers
    with pytest.raises(MetadataFetchError):
        await hanging


# ---- Logs ----


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (http_response(b"{}", status=302, headers={"Location": "https://x.test/"}), "redirect"),
        (http_response(b"{}", status=500), "http_error"),
        (http_response(b"<html>", headers={"Content-Type": "text/html"}), "not_json"),
        (http_response(b" " * 20000), "too_large"),
        (http_response(b"{}", headers={"Content-Encoding": "gzip"}), "encoding"),
        (
            json_response(document(client_id="https://assistant.example.test/x.json")),
            "invalid_document",
        ),
        (http_response(b"not json"), "invalid_document"),
    ],
)
async def test_a_failure_logs_one_fixed_reason_and_never_the_body_or_path(
    service: ClientMetadataService,
    internet: FakeInternet,
    caplog: pytest.LogCaptureFixture,
    reply: bytes,
    reason: str,
) -> None:
    caplog.set_level(logging.INFO)
    internet.serve(HOST, reply)
    with pytest.raises(MetadataFetchError):
        await service.resolve(CLIENT_URL, PERSON)
    failed = [r for r in caplog.records if r.getMessage() == "assistant metadata failed"]
    assert [(r.metadata_host, r.reason) for r in failed] == [(HOST, reason)]  # pyright: ignore[reportAttributeAccessIssue]
    assert "/client.json" not in caplog.text and "Location" not in caplog.text


async def test_private_address_and_timeout_failures_have_their_own_reasons(
    service: ClientMetadataService, internet: FakeInternet, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    internet.serve(HOST, http_response(b"{}"), address="10.0.0.4")
    with pytest.raises(MetadataFetchError):
        await service.resolve(CLIENT_URL, PERSON)
    slow = other_url(1, "slow.example.test")
    internet.serve("slow.example.test", http_response(b"{}"))
    internet.delay = 1.0
    quick = ClientMetadataService(internet.fetcher(total_timeout=0.1))
    with pytest.raises(MetadataFetchError):
        await quick.resolve(slow, PERSON)
    reasons = [r.reason for r in caplog.records if r.getMessage() == "assistant metadata failed"]  # pyright: ignore[reportAttributeAccessIssue]
    assert reasons == ["private_address", "timeout"]
