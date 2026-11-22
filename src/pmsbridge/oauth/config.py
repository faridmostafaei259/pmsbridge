"""Addresses and fixed values of the authorization server.

Every URL comes from `PUBLIC_BASE_URL` (the settings), never from the request, so a forged `Host`
header cannot change what is advertised or where a person is sent.
"""

from typing import Final
from urllib.parse import urlsplit

from pmsbridge.core.settings import get_settings

SCOPE: Final = "mcp"
MCP_PATH: Final = "/mcp"
AUTHORIZE_PATH: Final = "/oauth/authorize"
REGISTER_PATH: Final = "/oauth/register"
TOKEN_PATH: Final = "/oauth/token"  # noqa: S105  (a URL path, not a secret)
REVOKE_PATH: Final = "/oauth/revoke"
ACCESS_TOKEN_SECONDS: Final = 3600
GRANT_TYPES: Final = ("authorization_code", "refresh_token")
AUTH_METHODS: Final = ("none", "client_secret_post", "client_secret_basic")


def issuer() -> str:
    return get_settings().public_base_url


def mcp_resource() -> str:
    return issuer() + MCP_PATH


def url_for(path: str) -> str:
    return issuer() + path


def canonical_resource(url: str) -> str | None:
    """A resource address in comparable form (scheme and host lower case, no trailing slash).

    None when it is not a plain absolute address (it has a query or fragment, or no host).
    """
    parts = urlsplit(url)
    if parts.query or parts.fragment or not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def is_mcp_resource(url: str) -> bool:
    """True if `url` names this server's MCP endpoint."""
    return canonical_resource(url) == canonical_resource(mcp_resource())
