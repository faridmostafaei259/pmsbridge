import asyncio
import logging
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from pmsbridge.app import (
    FEATURE_MODULES,
    MAX_REQUEST_BODY_BYTES,
    SECURITY_HEADERS,
    RequestSizeLimitMiddleware,
    _RequestTooLarge,  # pyright: ignore[reportPrivateUsage]
    add_readiness_check,
    create_app,
)


@pytest.fixture
def feature_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A throwaway package with feature modules that behave in different ways."""
    pkg = "fake_features_for_app_tests"
    root = tmp_path / pkg
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "good.py").write_text(
        textwrap.dedent(
            """
            from fastapi import FastAPI

            def register(app: FastAPI) -> None:
                @app.get("/good")
                async def good() -> dict[str, str]:
                    return {"feature": "good"}
            """
        )
    )
    (root / "broken_import.py").write_text("raise ImportError('broken on purpose')\n")
    (root / "missing_dependency.py").write_text("import some_dependency_that_does_not_exist\n")
    (root / "no_register.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield pkg
    for name in [n for n in sys.modules if n == pkg or n.startswith(pkg + ".")]:
        del sys.modules[name]


def test_healthz_is_always_ok() -> None:
    client = TestClient(create_app(()))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_stays_ok_when_a_readiness_check_fails() -> None:
    app = create_app(())

    async def failing() -> tuple[bool, str]:
        return False, "down"

    add_readiness_check(app, "db", failing)
    assert TestClient(app).get("/healthz").status_code == 200


def test_readyz_with_no_checks_is_ready() -> None:
    response = TestClient(create_app(())).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": []}


def test_readyz_with_passing_checks_is_ready() -> None:
    app = create_app(())

    async def fine() -> tuple[bool, str]:
        return True, "ok"

    add_readiness_check(app, "database", fine)
    add_readiness_check(app, "vault", fine)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": [{"name": "database", "ok": True}, {"name": "vault", "ok": True}],
    }


def test_readyz_with_a_failing_check_is_503_and_hides_the_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(())

    async def fine() -> tuple[bool, str]:
        return True, "ok"

    async def failing() -> tuple[bool, str]:
        return False, "secret-looking internal detail"

    add_readiness_check(app, "database", fine)
    add_readiness_check(app, "migrations", failing)
    with caplog.at_level(logging.WARNING, logger="pmsbridge.app"):
        response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == [
        {"name": "database", "ok": True},
        {"name": "migrations", "ok": False},
    ]
    assert "secret-looking internal detail" not in response.text
    assert "migrations: secret-looking internal detail" in caplog.text


def test_readyz_treats_a_raising_check_as_failed_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(())

    async def exploding() -> tuple[bool, str]:
        raise RuntimeError("password=hunter2")

    add_readiness_check(app, "vault", exploding)
    with caplog.at_level(logging.WARNING, logger="pmsbridge.app"):
        response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == [{"name": "vault", "ok": False}]
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert "readiness check failed: vault: check raised an error" in caplog.text


def test_readyz_treats_a_slow_check_as_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("pmsbridge.app.READINESS_CHECK_TIMEOUT_SECONDS", 0.05)
    app = create_app(())

    async def slow() -> tuple[bool, str]:
        await asyncio.sleep(5)
        return True, "late"

    add_readiness_check(app, "slow", slow)
    with caplog.at_level(logging.WARNING, logger="pmsbridge.app"):
        response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == [{"name": "slow", "ok": False}]
    assert "readiness check failed: slow: timed out" in caplog.text


def test_missing_feature_module_is_skipped(
    feature_pkg: str, caplog: pytest.LogCaptureFixture
) -> None:
    modules = (f"{feature_pkg}.good", f"{feature_pkg}.does_not_exist_yet")
    with caplog.at_level(logging.INFO, logger="pmsbridge.app"):
        app = create_app(modules)
    assert TestClient(app).get("/good").json() == {"feature": "good"}
    assert f"features loaded: {feature_pkg}.good" in caplog.text
    assert f"not present yet: {feature_pkg}.does_not_exist_yet" in caplog.text


def test_module_that_raises_import_error_is_re_raised(feature_pkg: str) -> None:
    with pytest.raises(ImportError, match="broken on purpose"):
        create_app((f"{feature_pkg}.broken_import",))


def test_module_not_found_for_a_different_name_is_re_raised(feature_pkg: str) -> None:
    with pytest.raises(ModuleNotFoundError) as excinfo:
        create_app((f"{feature_pkg}.missing_dependency",))
    assert excinfo.value.name == "some_dependency_that_does_not_exist"


def test_missing_parent_package_is_re_raised(feature_pkg: str) -> None:
    with pytest.raises(ModuleNotFoundError) as excinfo:
        create_app((f"{feature_pkg}_absent.child",))
    assert excinfo.value.name == f"{feature_pkg}_absent"


def test_feature_without_register_is_an_error(feature_pkg: str) -> None:
    with pytest.raises(RuntimeError, match="does not define register"):
        create_app((f"{feature_pkg}.no_register",))


def test_default_feature_list_matches_the_wiring_convention() -> None:
    assert FEATURE_MODULES == (
        "pmsbridge.core.web",
        "pmsbridge.accounts.web",
        "pmsbridge.oauth.web",
        "pmsbridge.pms.connections",
        "pmsbridge.mcp.server",
    )


def test_default_app_starts_when_no_feature_exists_yet() -> None:
    client = TestClient(create_app())
    assert client.get("/healthz").status_code == 200


def test_security_headers_on_success_and_404() -> None:
    client = TestClient(create_app(()))
    for path in ("/healthz", "/readyz", "/no-such-page"):
        response = client.get(path)
        for name, value in SECURITY_HEADERS:
            assert response.headers[name] == value, (path, name)
    csp = client.get("/healthz").headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert client.get("/healthz").headers["X-Frame-Options"] == "DENY"
    assert client.get("/healthz").headers["X-Content-Type-Options"] == "nosniff"
    assert "Referrer-Policy" in client.get("/healthz").headers


def test_route_can_override_a_default_header() -> None:
    app = create_app(())

    @app.get("/page")
    async def page() -> JSONResponse:
        return JSONResponse({"ok": "yes"}, headers={"Cache-Control": "max-age=60"})

    response = TestClient(app).get("/page")
    assert response.headers["Cache-Control"] == "max-age=60"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_unhandled_error_has_no_traceback_and_keeps_security_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(())

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("database password is hunter2")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="pmsbridge.app"):
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"
    for leaked in ("Traceback", "RuntimeError", "hunter2", "boom", ".py"):
        assert leaked not in response.text
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value
    assert "unhandled error while serving GET /boom" in caplog.text


def test_http_exception_keeps_security_headers() -> None:
    app = create_app(())

    @app.get("/forbidden")
    async def forbidden() -> dict[str, str]:
        raise HTTPException(status_code=403, detail="no")

    response = TestClient(app).get("/forbidden")
    assert response.status_code == 403
    assert response.headers["X-Frame-Options"] == "DENY"


def test_api_docs_are_not_exposed() -> None:
    client = TestClient(create_app(()))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_create_app_returns_independent_apps() -> None:
    first, second = create_app(()), create_app(())
    assert isinstance(first, FastAPI)
    assert first is not second
    assert first.state.readiness_checks is not second.state.readiness_checks


def _app_with_body_routes() -> FastAPI:
    app = create_app(())

    @app.post("/raw")
    async def raw(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/json")
    async def parsed(payload: dict[str, str]) -> dict[str, int]:
        return {"keys": len(payload)}

    return app


def _chunks(count: int, size: int) -> Iterator[bytes]:
    for _ in range(count):
        yield b"x" * size


def test_the_body_limit_is_two_mebibytes() -> None:
    assert MAX_REQUEST_BODY_BYTES == 2 * 1024 * 1024


def test_a_body_at_the_limit_is_accepted() -> None:
    client = TestClient(_app_with_body_routes())
    response = client.post("/raw", content=b"x" * MAX_REQUEST_BODY_BYTES)
    assert response.status_code == 200
    assert response.json() == {"bytes": MAX_REQUEST_BODY_BYTES}


def test_a_declared_content_length_over_the_limit_gets_413_before_the_app_runs() -> None:
    reached: list[bool] = []
    app = _app_with_body_routes()

    @app.post("/spy")
    async def spy() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        reached.append(True)
        return {}

    response = TestClient(app).post("/spy", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert response.headers["connection"] == "close"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert reached == []


def test_a_chunked_body_over_the_limit_gets_413() -> None:
    client = TestClient(_app_with_body_routes())
    # No Content-Length: the body is sent in chunks.
    response = client.post("/raw", content=_chunks(MAX_REQUEST_BODY_BYTES // 1024 + 1, 1024))
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


def test_a_chunked_body_over_the_limit_gets_413_on_a_route_that_parses_json() -> None:
    client = TestClient(_app_with_body_routes())
    response = client.post(
        "/json",
        content=_chunks(MAX_REQUEST_BODY_BYTES // 1024 + 1, 1024),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_a_chunked_body_within_the_limit_is_accepted() -> None:
    client = TestClient(_app_with_body_routes())
    response = client.post("/raw", content=_chunks(MAX_REQUEST_BODY_BYTES // 1024, 1024))
    assert response.status_code == 200
    assert response.json() == {"bytes": MAX_REQUEST_BODY_BYTES}


def test_a_streamed_body_is_not_read_past_the_limit() -> None:
    """The middleware stops pulling chunks once the limit is passed (nothing is buffered)."""
    limit = 100
    pulled = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"x" * 40, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    async def greedy_app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            await receive()

    middleware = RequestSizeLimitMiddleware(greedy_app, max_bytes=limit)
    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    assert pulled == 3  # 40, 80, then 120 bytes: stopped at the first chunk over the limit
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_requests_without_a_body_are_not_affected() -> None:
    client = TestClient(create_app(()))
    assert client.get("/healthz").status_code == 200
    assert client.post("/healthz", content=b"").status_code == 405


def test_a_chunked_body_over_the_limit_read_in_a_task_group_gets_413() -> None:
    app = _app_with_body_routes()

    @app.post("/grouped")
    async def grouped(request: Request) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        async def read() -> None:
            await request.body()

        async with anyio.create_task_group() as group:
            group.start_soon(read)
        return {}

    response = TestClient(app).post(
        "/grouped", content=_chunks(MAX_REQUEST_BODY_BYTES // 1024 + 1, 1024)
    )
    assert response.status_code == 413


def _oversized_stream_scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    return {"type": "http", "method": "POST", "path": "/", "headers": headers}


async def _greedy_app(scope: Scope, receive: Receive, send: Send) -> None:
    while True:
        await receive()


@pytest.mark.parametrize("declared", [b"-1", b"abc", b"10, 10", b""])
def test_an_unusable_content_length_does_not_disable_the_streamed_count(declared: bytes) -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 40, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = RequestSizeLimitMiddleware(_greedy_app, max_bytes=100)
    scope = _oversized_stream_scope([(b"content-length", declared)])
    asyncio.run(middleware(scope, receive, send))
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413


def test_a_body_over_the_limit_after_the_response_started_propagates() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 40, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    async def answers_then_reads(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            await receive()

    middleware = RequestSizeLimitMiddleware(answers_then_reads, max_bytes=100)
    with pytest.raises(_RequestTooLarge):
        asyncio.run(middleware(_oversized_stream_scope([]), receive, send))
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert sent[0]["status"] == 200
