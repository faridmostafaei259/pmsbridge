"""Discovery documents: authorization server metadata (RFC 8414) and protected resource metadata
(RFC 9728).

Both are public JSON, cached for five minutes, carry no cookies, and are built from the settings
only. The protected resource (the MCP endpoint) has a path, so its document is served at the plain
well-known address and at the path-suffixed one the MCP specification asks clients to try first.
The issuer has no path, so the authorization server document has one address.
"""

from typing import Final

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pmsbridge.oauth.config import (
    AUTH_METHODS,
    AUTHORIZE_PATH,
    GRANT_TYPES,
    MCP_PATH,
    REGISTER_PATH,
    REVOKE_PATH,
    SCOPE,
    TOKEN_PATH,
    issuer,
    mcp_resource,
    url_for,
)

CACHE_CONTROL: Final = "public, max-age=300"
# Browser-based assistants read these public documents from another site; nothing else is opened up.
_CORS: Final = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "mcp-protocol-version, content-type",
    "Access-Control-Max-Age": "86400",
}


def authorization_server_metadata() -> dict[str, object]:
    return {
        "issuer": issuer(),
        "authorization_endpoint": url_for(AUTHORIZE_PATH),
        "token_endpoint": url_for(TOKEN_PATH),
        "registration_endpoint": url_for(REGISTER_PATH),
        "revocation_endpoint": url_for(REVOKE_PATH),
        "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS),
        "response_types_supported": ["code"],
        "grant_types_supported": list(GRANT_TYPES),
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
        "scopes_supported": [SCOPE],
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
    }


def protected_resource_metadata() -> dict[str, object]:
    return {
        "resource": mcp_resource(),
        "authorization_servers": [issuer()],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "pmsbridge",
    }


def _document(body: dict[str, object]) -> Response:
    return JSONResponse(body, headers={"Cache-Control": CACHE_CONTROL, **_CORS})


def register_routes(app: FastAPI) -> None:
    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    async def authorization_server(_request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return _document(authorization_server_metadata())

    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.get(f"/.well-known/oauth-protected-resource{MCP_PATH}", include_in_schema=False)
    async def protected_resource(_request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return _document(protected_resource_metadata())

    @app.options("/.well-known/oauth-authorization-server", include_in_schema=False)
    @app.options("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.options(f"/.well-known/oauth-protected-resource{MCP_PATH}", include_in_schema=False)
    async def preflight(_request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(status_code=204, headers=_CORS)
