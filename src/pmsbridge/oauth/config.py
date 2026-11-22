"""Addresses and fixed values of the authorization server.

Every URL comes from `PUBLIC_BASE_URL` (the settings), never from the request, so a forged `Host`
header cannot change what is advertised or where a person is sent.
"""

from typing import Final

from pmsbridge.core.settings import get_settings

SCOPE: Final = "mcp"
MCP_PATH: Final = "/mcp"
AUTHORIZE_PATH: Final = "/oauth/authorize"
REGISTER_PATH: Final = "/oauth/register"
TOKEN_PATH: Final = "/oauth/token"  # noqa: S105  (a URL path, not a secret)
GRANT_TYPES: Final = ("authorization_code", "refresh_token")
AUTH_METHODS: Final = ("none", "client_secret_post", "client_secret_basic")


def issuer() -> str:
    return get_settings().public_base_url


def mcp_resource() -> str:
    return issuer() + MCP_PATH


def url_for(path: str) -> str:
    return issuer() + path
