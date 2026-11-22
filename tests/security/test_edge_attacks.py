"""Attacks on the application edge that do not need a running stack.

The proxy tests drive the same Uvicorn configuration the container uses (`build_config`) with raw
ASGI scopes, so they show exactly which sender is allowed to set the client address.
"""

import logging
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from pmsbridge.__main__ import DEFAULT_TRUSTED_PROXIES, build_config
from pmsbridge.app import INTERNAL_ERROR_BODY, SECURITY_HEADERS, add_readiness_check, create_app

CADDY = "172.31.250.10"
NEIGHBOUR = "172.31.250.11"
GATEWAY = "172.31.250.1"


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(_: Message) -> None:
    return None


async def seen_by_app(trusted: str | None, peer: str, headers: list[tuple[bytes, bytes]]) -> Scope:
    """Run one request through Uvicorn's proxy handling and return the scope the app received."""
    captured: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        captured.append(scope)

    env = {} if trusted is None else {"TRUSTED_PROXIES": trusted}
    config = build_config(cast(FastAPI, inner), env)
    config.load()
    assert config.loaded_app is not None
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 40000),
        "server": ("app", 8000),
    }
    await config.loaded_app(scope, _receive, _send)
    assert captured
    return captured[0]


FORGED = [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-proto", b"https")]


async def test_a_neighbour_container_cannot_set_the_client_address() -> None:
    scope = await seen_by_app(CADDY, NEIGHBOUR, FORGED)
    assert scope["client"] == (NEIGHBOUR, 40000)
    assert scope["scheme"] == "http"


async def test_the_host_gateway_cannot_set_the_client_address() -> None:
    """A request from the host (to the app's bridge address) arrives from the gateway address."""
    scope = await seen_by_app(CADDY, GATEWAY, FORGED)
    assert scope["client"] == (GATEWAY, 40000)
    assert scope["scheme"] == "http"


async def test_the_default_trusts_nobody_but_loopback() -> None:
    assert DEFAULT_TRUSTED_PROXIES == "127.0.0.1"
    scope = await seen_by_app(None, CADDY, FORGED)
    assert scope["client"] == (CADDY, 40000)


async def test_only_the_configured_proxy_may_set_the_client_address() -> None:
    scope = await seen_by_app(CADDY, CADDY, [(b"x-forwarded-for", b"203.0.113.9")])
    assert scope["client"][0] == "203.0.113.9"


async def test_addresses_a_visitor_prepended_are_not_believed_behind_the_trusted_proxy() -> None:
    """Caddy appends the real peer. Anything the visitor wrote before it must not win."""
    scope = await seen_by_app(CADDY, CADDY, [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")])
    assert scope["client"][0] == "203.0.113.9"


async def test_the_forwarded_header_of_rfc_7239_is_ignored() -> None:
    scope = await seen_by_app(CADDY, CADDY, [(b"forwarded", b"for=6.6.6.6;proto=https")])
    assert scope["client"][0] == CADDY
    assert scope["scheme"] == "http"


async def test_a_trusted_list_with_spaces_and_empty_entries_still_trusts_only_listed_hosts() -> (
    None
):
    scope = await seen_by_app(f" , {CADDY} ,, ", NEIGHBOUR, FORGED)
    assert scope["client"] == (NEIGHBOUR, 40000)


async def test_a_subnet_entry_trusts_exactly_that_subnet() -> None:
    inside = await seen_by_app("10.1.0.0/24", "10.1.0.77", FORGED)
    outside = await seen_by_app("10.1.0.0/24", "10.1.1.77", FORGED)
    assert inside["client"][0] == "6.6.6.6"
    assert outside["client"][0] == "10.1.1.77"


# ---- Error responses ----


def test_errors_never_reveal_internals_and_always_carry_the_security_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(
            "connect failed: postgres://user:hunter2@10.0.0.9/db /srv/app/secrets.py"
        )

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == INTERNAL_ERROR_BODY
    for leaked in ("hunter2", "10.0.0.9", "/srv/app", "RuntimeError", "Traceback", "boom"):
        assert leaked not in response.text
        assert all(leaked not in value for value in response.headers.values())
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/does-not-exist", 404),
        ("GET", "/healthz/", 307),
        ("POST", "/healthz", 405),
        ("PUT", "/healthz", 405),
        ("DELETE", "/healthz", 405),
        ("PATCH", "/readyz", 405),
        ("OPTIONS", "/healthz", 405),
        ("GET", "/docs", 404),
        ("GET", "/redoc", 404),
        ("GET", "/openapi.json", 404),
        ("GET", "/docs/oauth2-redirect", 404),
        ("GET", "/..%2f..%2fetc/passwd", 404),
        ("GET", "/%00", 404),
        ("GET", "/healthz;jsessionid=1", 404),
    ],
)
def test_unusual_requests_get_plain_answers_with_security_headers(
    method: str, path: str, expected: int
) -> None:
    client = TestClient(create_app(()), follow_redirects=False)
    response = client.request(method, path)
    assert response.status_code == expected
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value
    for leaked in ("Traceback", 'File "', "starlette", "fastapi", "uvicorn"):
        assert leaked not in response.text
    assert "server" not in {name.lower() for name in response.headers}


def test_the_framework_version_is_not_advertised() -> None:
    response = TestClient(create_app(())).get("/healthz")
    blob = " ".join(f"{k}: {v}" for k, v in response.headers.items()).lower()
    assert "uvicorn" not in blob
    assert "python" not in blob
    assert "x-powered-by" not in blob


def test_the_readiness_detail_stays_out_of_the_response() -> None:
    app = create_app(())

    async def failing() -> tuple[bool, str]:
        return False, "database query failed: OperationalError: unable to open /data/pmsbridge.db"

    add_readiness_check(app, "database", failing)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": [{"name": "database", "ok": False}]}
    assert "OperationalError" not in response.text
    assert "/data" not in response.text
