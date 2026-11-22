"""Helpers shared by the OAuth tests: constants, PKCE, signing in, the consent form."""

import base64
import hashlib
import html
import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx

BASE_URL = "https://pms.example.test"
EMAIL = "ada@example.test"
PASSWORD = "fake-passphrase-for-tests"  # noqa: S105
CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
CALLBACK = "https://assistant.example.test/callback"
VERIFIER = "fake-verifier-0123456789-0123456789-0123456789-abcdef"
Browser = Callable[[str], httpx.AsyncClient]


def challenge_for(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def page_token(text: str) -> str:
    match = CSRF_FIELD.search(text)
    assert match, "the page has no CSRF field"
    return match.group(1)


async def sign_in(
    client: httpx.AsyncClient, email: str = EMAIL, password: str = PASSWORD
) -> httpx.Response:
    page = await client.get("/login")
    return await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": page_token(page.text)},
        headers={"Origin": BASE_URL},
    )


async def register_client(
    client: httpx.AsyncClient, **overrides: object
) -> dict[str, str | list[str]]:
    body: dict[str, object] = {
        "client_name": "Example Assistant",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        **overrides,
    }
    response = await client.post("/oauth/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def authorize_params(client_id: str, **overrides: str | None) -> dict[str, str]:
    params: dict[str, str | None] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge_for(),
        "code_challenge_method": "S256",
        "state": "state-123",
        **overrides,
    }
    return {name: value for name, value in params.items() if value is not None}


def location_query(response: httpx.Response) -> dict[str, str]:
    parts = urlsplit(response.headers["location"])
    return {name: values[0] for name, values in parse_qs(parts.query).items()}


def link_query(text: str) -> dict[str, str]:
    """The query of the "Return to" link on a request error page."""
    match = re.search(r'<a class="button" href="([^"]+)"', text)
    assert match, "the page has no return link"
    query = urlsplit(html.unescape(match.group(1))).query
    return {name: values[0] for name, values in parse_qs(query).items()}


async def approve(
    client: httpx.AsyncClient, params: dict[str, str], decision: str = "approve", **extra: str
) -> httpx.Response:
    """Open the consent page for `params` and submit it the way a browser would."""
    page = await client.get("/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    hidden = {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input type="hidden" name="(?!csrf_token)([^"]+)" value="([^"]*)"', page.text
        )
    }
    return await client.post(
        "/oauth/authorize",
        data={**hidden, "csrf_token": page_token(page.text), "decision": decision, **extra},
        headers={"Origin": BASE_URL},
    )


BOTH_GRANTS = ["authorization_code", "refresh_token"]
TOKEN_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


async def get_code(browser: httpx.AsyncClient, client_id: str, **overrides: str | None) -> str:
    response = await approve(browser, authorize_params(client_id, **overrides))
    assert response.status_code == 303, response.text
    return location_query(response)["code"]


def code_form(
    client_id: str, code: str, *, anonymous: bool = False, **overrides: str | None
) -> dict[str, str]:
    form: dict[str, str | None] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "code_verifier": VERIFIER,
        "client_id": None if anonymous else client_id,
        **overrides,
    }
    return {name: value for name, value in form.items() if value is not None}


async def token_request(
    client: httpx.AsyncClient, form: dict[str, str], headers: dict[str, str] | None = None
) -> httpx.Response:
    return await client.post("/oauth/token", data=form, headers=headers)


async def connect(
    browser: httpx.AsyncClient, api: httpx.AsyncClient, **registration: object
) -> tuple[str, dict[str, str]]:
    """Register a client with refresh tokens, approve it and exchange the code.

    Returns the client id and the token response.
    """
    registration.setdefault("grant_types", BOTH_GRANTS)
    client_id = str((await register_client(api, **registration))["client_id"])
    code = await get_code(browser, client_id)
    response = await token_request(api, code_form(client_id, code))
    assert response.status_code == 200, response.text
    return client_id, response.json()  # type: ignore[no-any-return]


async def register_secret_client(
    api: httpx.AsyncClient, method: str = "client_secret_post"
) -> tuple[str, str]:
    """Register a confidential client that may also refresh; returns (client id, secret)."""
    created = await register_client(
        api,
        grant_types=BOTH_GRANTS,
        token_endpoint_auth_method=method,
    )
    return str(created["client_id"]), str(created["client_secret"])
