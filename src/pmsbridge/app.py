"""Application factory for the pmsbridge web service.

`create_app` builds the FastAPI application, adds the liveness and readiness endpoints, installs the
security-header middleware and registers the feature modules that exist.

Features are registered by discovery. Each feature module in `FEATURE_MODULES` may define
`register(app: FastAPI) -> None`. A module that does not exist yet is skipped. Any other import
problem (a missing dependency, a bug at import time) is raised so that it fails loudly at startup.
"""

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pmsbridge import __version__

logger = logging.getLogger(__name__)

# The fixed list of feature modules. Each owner implements `register(app)` in its own module.
FEATURE_MODULES: tuple[str, ...] = (
    "pmsbridge.accounts.web",
    "pmsbridge.oauth.web",
    "pmsbridge.pms.connections",
    "pmsbridge.mcp.server",
)

# A readiness probe reports (ok, detail). The detail goes to the logs, never to the HTTP response.
ReadinessProbe = Callable[[], Awaitable[tuple[bool, str]]]
# What is stored on `app.state.readiness_checks`: an async callable returning (name, ok, detail).
ReadinessCheck = Callable[[], Awaitable[tuple[str, bool, str]]]

# Upper bound for one readiness check, so a stuck dependency cannot hang the endpoint.
READINESS_CHECK_TIMEOUT_SECONDS = 5.0

# Default response headers. Routes that set their own value for a header (for example a dashboard
# page that needs a looser Content-Security-Policy) keep it.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    (
        "Content-Security-Policy",
        "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    ),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Cache-Control", "no-store"),
)

INTERNAL_ERROR_BODY = {
    "error": "internal_error",
    "message": "Something went wrong on the server. The operator can find details in the logs.",
}


class EdgeMiddleware:
    """Adds the default security headers and turns unhandled errors into a plain 500.

    It is written as plain ASGI middleware so that streaming responses are not buffered. Unhandled
    exceptions are logged on the server and never shown to the client (no traceback, no message).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            logger.exception("unhandled error while serving %s %s", scope["method"], scope["path"])
            if started:
                # The response is already partly sent. Let the server close the connection.
                raise
            response = JSONResponse(INTERNAL_ERROR_BODY, status_code=500)
            await response(scope, receive, send_with_headers)


class _NamedCheck:
    """A readiness probe that keeps its registered name, even when the probe fails or hangs."""

    def __init__(self, name: str, probe: ReadinessProbe) -> None:
        self.name = name
        self._probe = probe

    async def __call__(self) -> tuple[str, bool, str]:
        ok, detail = await self._probe()
        return self.name, ok, detail


def _readiness_checks(app: FastAPI) -> list[ReadinessCheck]:
    checks = cast("list[ReadinessCheck]", app.state.readiness_checks)
    return checks


def add_readiness_check(app: FastAPI, name: str, check: ReadinessProbe) -> None:
    """Register a readiness check. Feature modules call this from their `register(app)`.

    `check` is an async callable that returns `(ok, detail)`. It should not raise for an ordinary
    failure, but if it does, or takes longer than `READINESS_CHECK_TIMEOUT_SECONDS`, the check
    counts as failed. `detail` is written to the logs when the check fails.
    """

    _readiness_checks(app).append(_NamedCheck(name, check))


async def _run_check(check: ReadinessCheck) -> tuple[str, bool, str]:
    label = check.name if isinstance(check, _NamedCheck) else "unnamed check"
    try:
        async with asyncio.timeout(READINESS_CHECK_TIMEOUT_SECONDS):
            return await check()
    except TimeoutError:
        return label, False, f"timed out after {READINESS_CHECK_TIMEOUT_SECONDS:g} seconds"
    except Exception:
        logger.exception("readiness check raised an error")
        return label, False, "check raised an error"


def _load_features(app: FastAPI, modules: Sequence[str]) -> None:
    loaded: list[str] = []
    not_present: list[str] = []
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Skip only when the feature module itself is absent. A missing dependency inside an
            # existing module has a different `name` and must surface.
            if exc.name != module_name:
                raise
            not_present.append(module_name)
            continue
        register: object = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(f"feature module {module_name} does not define register(app)")
        cast("Callable[[FastAPI], None]", register)(app)
        loaded.append(module_name)
    logger.info(
        "features loaded: %s; not present yet: %s",
        ", ".join(loaded) or "none",
        ", ".join(not_present) or "none",
    )


def create_app(feature_modules: Sequence[str] = FEATURE_MODULES) -> FastAPI:
    """Build the application. Safe to call more than once (each call returns a fresh app)."""
    app = FastAPI(
        title="pmsbridge",
        version=__version__,
        debug=False,
        # The interactive API docs are not part of the product surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.readiness_checks = []
    app.add_middleware(EdgeMiddleware)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness: answers 200 as long as the process is running."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Readiness: 200 when every registered check passes, 503 otherwise."""
        results = await asyncio.gather(*(_run_check(check) for check in _readiness_checks(app)))
        for name, ok, detail in results:
            if not ok:
                logger.warning("readiness check failed: %s: %s", name, detail)
        all_ok = all(ok for _, ok, _ in results)
        body = {
            "status": "ready" if all_ok else "not_ready",
            "checks": [{"name": name, "ok": ok} for name, ok, _ in results],
        }
        return JSONResponse(body, status_code=200 if all_ok else 503)

    _load_features(app, feature_modules)
    return app
