"""The MCP endpoint at `/mcp`: stateless Streamable HTTP, Bearer tokens only.

Every request must carry an access token from the OAuth server in this application. The token is
checked with `validate_access_token` (expiry, revocation, audience, the person being active), so
revoking a grant or disabling a person takes effect on the next request. A missing or unusable
token gets 401 with a `WWW-Authenticate` header that points to the protected resource metadata,
which is how an AI assistant finds out where to sign in. There are no cookies and no CSRF
protection because nothing but the Authorization header is accepted; no CORS headers are sent,
because assistants call the endpoint from their servers, not from a web page.

Tools are registered by discovery: every `pmsbridge.mcp.tools.<domain>` module that exists and
exports `register(mcp)`. Only a missing tool module is skipped; any other import error is raised.
"""

import importlib
import logging
import pkgutil
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pmsbridge import __version__
from pmsbridge.core import audit
from pmsbridge.mcp import tools as tools_package
from pmsbridge.mcp.context import PrincipalToken
from pmsbridge.oauth.config import MCP_PATH, SCOPE, issuer, mcp_resource, url_for
from pmsbridge.oauth.tokens import validate_access_token
from pmsbridge.pms.base import Provider

logger = logging.getLogger(__name__)

TOOL_DOMAINS: tuple[str, ...] = (
    "connections",
    "listings",
    "reservations",
    "calendar",
    "guests",
    "inbox",
    "reviews",
    "owners",
    "financials",
    "tasks",
)
INSTRUCTIONS = (
    "pmsbridge gives access to a company's property management system (reservations, listings, "
    "calendar, guests, messages, reviews, owners, payments, tasks). Call list_connections to see "
    "which connections you may use. Tools that change data only work on connections where an "
    "administrator has allowed changes."
)
HTTP_UNAUTHORIZED = 401
HTTP_METHOD_NOT_ALLOWED = 405
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"


class PmsbridgeTokenVerifier:
    """Checks a Bearer token with the OAuth server's own validation (one database lookup)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = await validate_access_token(token)
        if principal is None:
            return None
        return PrincipalToken(
            token="",  # the value is not needed after the check and is not kept
            client_id=principal.client_id,
            scopes=principal.scope.split(),
            expires_at=int(principal.expires_at.timestamp()),
            resource=mcp_resource(),
            subject=str(principal.user_id),
            principal=principal,
        )


def www_authenticate(token_sent: bool) -> str:
    """The challenge for a 401: where to find the metadata, and the error when a token was sent."""
    value = f'Bearer resource_metadata="{url_for(PROTECTED_RESOURCE_METADATA_PATH + MCP_PATH)}"'
    return value + ', error="invalid_token"' if token_sent else value


class _McpEndpoint:
    """The SDK's endpoint, with the 401 challenge written the way this server documents it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] != "POST":
            # Stateless: the server never starts a message stream or keeps a session, so there is
            # nothing to open with GET or to end with DELETE (the transport spec allows 405 here).
            await Response(
                "Method not allowed.",
                status_code=HTTP_METHOD_NOT_ALLOWED,
                headers={"Allow": "POST"},
            )(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization", "")
        token_sent = authorization.lower().startswith("bearer ")

        async def challenge(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == HTTP_UNAUTHORIZED:
                headers = MutableHeaders(scope=message)
                headers["WWW-Authenticate"] = www_authenticate(token_sent)
                headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, challenge)


def load_tool_modules(mcp: MCPServer, names: Sequence[str]) -> list[str]:
    """Import each tool module that exists and call its `register(mcp)`; returns those loaded.

    A module that does not exist yet is skipped. Any other import problem is raised.
    """
    loaded: list[str] = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            continue
        register_tools: object = getattr(module, "register", None)
        if not callable(register_tools):
            raise RuntimeError(f"tool module {name} does not define register(mcp)")
        cast("Callable[[MCPServer], None]", register_tools)(mcp)
        loaded.append(name)
    return loaded


def tool_module_names() -> list[str]:
    """The shared domain modules, then any provider-specific ones (`<provider>_<area>`)."""
    prefix = tools_package.__name__
    names = [f"{prefix}.{domain}" for domain in TOOL_DOMAINS]
    prefixes = tuple(f"{provider.value}_" for provider in Provider)
    found = sorted(
        info.name
        for info in pkgutil.iter_modules(tools_package.__path__)
        if info.name.startswith(prefixes)
    )
    return names + [f"{prefix}.{name}" for name in found]


def build_server(module_names: Sequence[str] | None = None) -> MCPServer:
    """The MCP server with auth settings and every discovered tool registered."""
    server = MCPServer(
        "pmsbridge",
        instructions=INSTRUCTIONS,
        version=__version__,
        token_verifier=PmsbridgeTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer()),
            resource_server_url=AnyHttpUrl(mcp_resource()),
            required_scopes=[SCOPE],
            validate_token_resource=True,
        ),
        warn_on_duplicate_tools=True,
    )
    names = tool_module_names() if module_names is None else module_names
    loaded = load_tool_modules(server, names)
    logger.info("tool modules loaded: %s", ", ".join(loaded) or "none")
    return server


def build_endpoint(server: MCPServer) -> ASGIApp:
    """The ASGI application that serves `/mcp` for `server` (stateless, JSON responses)."""
    sdk_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        # Host checks guard servers on a developer's machine. Here the only credential is the
        # Authorization header, which a rebinding page cannot make a browser send.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return _McpEndpoint(sdk_app)


def _run_on_lifespan(app: FastAPI, server: MCPServer) -> None:
    """Start the transport's task group and the audit cleanup with the application."""
    wrapped = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
        async with wrapped(application), server.session_manager.run():
            purge = audit.start_purge_task()
            try:
                yield
            finally:
                await audit.stop_purge_task(purge)

    app.router.lifespan_context = lifespan


def register(app: FastAPI) -> None:
    server = build_server()
    endpoint = build_endpoint(server)
    app.router.routes.append(Route(MCP_PATH, endpoint=endpoint))
    app.state.mcp_server = server
    _run_on_lifespan(app, server)
    logger.info(
        "mcp endpoint ready",
        extra={"path": MCP_PATH, "tools": len(server._tool_manager.list_tools())},  # pyright: ignore[reportPrivateUsage]
    )
