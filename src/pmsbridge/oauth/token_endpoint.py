"""The token endpoint `POST /oauth/token` and the revocation endpoint `POST /oauth/revoke`.

Both take `application/x-www-form-urlencoded` bodies of at most 8 KiB, never repeat a parameter,
and answer with `Cache-Control: no-store` and JSON errors that name only the OAuth error code and
a fixed sentence (RFC 6749 section 5.2). The client proves who it is the way it registered:
nothing but its `client_id` for a public client (PKCE protects the code), `client_secret` in the
body, or HTTP Basic. Secrets are compared as SHA-256 hashes in constant time, and an unknown
client costs the same work as a wrong secret.

`authorization_code` exchanges a code (see `grants.redeem_code`) for an access token and, if the
assistant registered for it, a refresh token. `refresh_token` rotates the refresh token (see
`tokens.py`). Failed requests are counted per visitor address, and a client that fails the secret
check of a confidential client is also counted under that client's id; a key that fails too often is
refused for a few minutes with 429. A public client has no secret to guess, so its claimed id never
blocks a request: anyone can claim any public client id, and counting it would let a stranger lock
the real assistant out. A client that named itself with a web address (`client_metadata.py`) is a
public client found by that address among the stored clients; this endpoint never fetches anything.
A grant refreshed more than 10 times in a minute is told to wait.
"""

import base64
import binascii
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, unquote_plus

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pmsbridge.accounts.ratelimit import FailureLimiter, client_address_key, is_shared_address
from pmsbridge.accounts.sessions import hash_token
from pmsbridge.oauth import clients, grants, tokens
from pmsbridge.oauth.config import REVOKE_PATH, SCOPE, TOKEN_PATH, is_mcp_resource
from pmsbridge.oauth.db import OAuthClient
from pmsbridge.oauth.metadata_fetch import MAX_URL_LENGTH

logger = logging.getLogger("pmsbridge.oauth")

MAX_BODY_BYTES: Final = 8 * 1024
MAX_FIELDS: Final = 20
MAX_CLIENT_ID: Final = MAX_URL_LENGTH  # a registered id is shorter; a web address up to this
MAX_SECRET: Final = 256
CLIENT_MAX_FAILURES: Final = 20
ADDRESS_MAX_FAILURES: Final = 30
SHARED_ADDRESS_MAX_FAILURES: Final = 300
_FORM_TYPE: Final = "application/x-www-form-urlencoded"
_NO_STORE: Final = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_COUNTED_ERRORS: Final = ("invalid_client", "invalid_grant")
_NO_SECRET: Final = hash_token("no client secret was presented")


class TokenError(Exception):
    """An error answer: the OAuth error code and a fixed description (never the caller's input)."""

    def __init__(
        self,
        error: str,
        description: str,
        status: int = 400,
        *,
        basic_challenge: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.basic_challenge = basic_challenge
        self.retry_after = retry_after


@dataclass(frozen=True)
class Credentials:
    client_id: str
    secret: str | None
    via_header: bool


class TokenLimiter:
    """Failures per visitor address (private addresses share a larger limit) and per client.

    The client key is used only for confidential clients, and only their failed secret checks
    count towards it.
    """

    def __init__(self) -> None:
        self.client = FailureLimiter(CLIENT_MAX_FAILURES)
        self.address = FailureLimiter(ADDRESS_MAX_FAILURES)
        self.shared = FailureLimiter(SHARED_ADDRESS_MAX_FAILURES)

    def _by_address(self, address: str) -> FailureLimiter:
        return self.shared if is_shared_address(address) else self.address

    def reserve_address(self, address: str) -> int:
        return self._by_address(address).reserve(address)

    def reserve_client(self, client_id: str) -> int:
        return self.client.reserve(client_id)

    def settle(
        self,
        address: str,
        client_id: str | None,
        *,
        address_failed: bool,
        client_failed: bool,
    ) -> None:
        """Turn the pending slots into failures or give them back."""
        by_address = self._by_address(address)
        if address_failed:
            by_address.record_failure(address)
        else:
            by_address.release(address)
        if client_id is not None:
            if client_failed:
                self.client.record_failure(client_id)
            else:
                self.client.release(client_id)


def _error_response(error: TokenError, headers: dict[str, str] | None = None) -> Response:
    extra = dict(headers or {})
    if error.retry_after is not None:
        extra["Retry-After"] = str(error.retry_after)
    if error.basic_challenge:
        extra["WWW-Authenticate"] = 'Basic realm="pmsbridge", charset="UTF-8"'
    return JSONResponse(
        {"error": error.error, "error_description": error.description},
        status_code=error.status,
        headers={**_NO_STORE, **extra},
    )


async def _read_form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != _FORM_TYPE:
        raise TokenError("invalid_request", f"Send the request as {_FORM_TYPE}.")
    declared = request.headers.get("content-length", "")
    body = bytearray()
    if declared.isdecimal() and int(declared) > MAX_BODY_BYTES:
        raise TokenError("invalid_request", "The request is too large.", 413)
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise TokenError("invalid_request", "The request is too large.", 413)
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=MAX_FIELDS)
    except ValueError:
        raise TokenError("invalid_request", "The request body could not be read.") from None
    form: dict[str, str] = {}
    for name, value in pairs:
        if name in form:
            raise TokenError("invalid_request", "A parameter was sent more than once.")
        form[name] = value
    return form


def _credentials(request: Request, form: dict[str, str]) -> Credentials:
    header = request.headers.get("authorization")
    if header is None:
        client_id, secret = form.get("client_id", ""), form.get("client_secret")
        if not client_id or len(client_id) > MAX_CLIENT_ID or len(secret or "") > MAX_SECRET:
            raise TokenError("invalid_client", "Client authentication failed.")
        return Credentials(client_id, secret, via_header=False)
    if "client_secret" in form:
        raise TokenError("invalid_request", "Use only one way to authenticate the client.")
    failed = TokenError(
        "invalid_client", "Client authentication failed.", 401, basic_challenge=True
    )
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or len(encoded) > 2 * (MAX_CLIENT_ID + MAX_SECRET):
        raise failed
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise failed from None
    raw_id, separator, raw_secret = decoded.partition(":")
    client_id, secret = unquote_plus(raw_id), unquote_plus(raw_secret)
    if not separator or not client_id or len(client_id) > MAX_CLIENT_ID or len(secret) > MAX_SECRET:
        raise failed
    if form.get("client_id", client_id) != client_id:
        raise failed
    return Credentials(client_id, secret, via_header=True)


def _authenticate(client: OAuthClient | None, credentials: Credentials) -> OAuthClient:
    """The client, if it proved its identity the way it registered; otherwise `invalid_client`."""
    presented = _NO_SECRET if credentials.secret is None else hash_token(credentials.secret)
    stored = (client.client_secret_hash if client else None) or _NO_SECRET
    secret_matches = secrets.compare_digest(stored, presented) and stored != _NO_SECRET
    method = client.token_endpoint_auth_method if client else ""
    allowed = (
        (method == "none" and credentials.secret is None and not credentials.via_header)
        or (method == "client_secret_post" and secret_matches and not credentials.via_header)
        or (method == "client_secret_basic" and secret_matches and credentials.via_header)
    )
    if client is None or not allowed:
        raise TokenError(
            "invalid_client",
            "Client authentication failed.",
            401 if credentials.via_header else 400,
            basic_challenge=credentials.via_header,
        )
    return client


def _check_resource(form: dict[str, str]) -> None:
    resource = form.get("resource")
    if resource is not None and not is_mcp_resource(resource):
        raise TokenError("invalid_target", "The resource must be this server's MCP address.")


def _required(form: dict[str, str], name: str, limit: int) -> str:
    value = form.get(name, "")
    if not value or len(value) > limit:
        raise TokenError("invalid_request", f"{name} is required.")
    return value


def _require_grant_type(client: OAuthClient, grant_type: str) -> None:
    if grant_type not in client.grant_types:
        raise TokenError("unauthorized_client", "This client may not use this grant type.")


async def _exchange_code(form: dict[str, str], client: OAuthClient) -> tokens.IssuedTokens:
    code = _required(form, "code", 256)
    redirect_uri = _required(form, "redirect_uri", 2048)
    verifier = _required(form, "code_verifier", 128)
    _require_grant_type(client, "authorization_code")
    _check_resource(form)
    redeemed = await grants.redeem_code(code, client.client_id, redirect_uri, verifier)
    issued = None
    if redeemed is not None:
        issued = await tokens.issue_for_code(
            redeemed, with_refresh="refresh_token" in client.grant_types
        )
    if issued is None:
        raise TokenError("invalid_grant", "The authorization code is invalid, expired or used.")
    return issued


async def _refresh(form: dict[str, str], client: OAuthClient) -> tokens.IssuedTokens:
    token = _required(form, "refresh_token", 256)
    _require_grant_type(client, "refresh_token")
    _check_resource(form)
    if set(form.get("scope", SCOPE).split()) - {SCOPE}:
        raise TokenError("invalid_scope", "Only the scope mcp is available.")
    issued = await tokens.rotate_refresh_token(token, client.client_id)
    if isinstance(issued, tokens.RefreshLimited):
        raise TokenError(
            "temporarily_unavailable",
            "This approval was refreshed too often. Try again later.",
            429,
            retry_after=issued.retry_after,
        )
    if issued is None:
        raise TokenError("invalid_grant", "The refresh token is invalid, expired or revoked.")
    return issued


async def _issue(form: dict[str, str], client: OAuthClient) -> Response:
    grant_type = form.get("grant_type", "")
    if grant_type == "authorization_code":
        issued = await _exchange_code(form, client)
    elif grant_type == "refresh_token":
        issued = await _refresh(form, client)
    else:
        raise TokenError("unsupported_grant_type", "Use authorization_code or refresh_token.")
    logger.info(
        "assistant tokens issued", extra={"client_id": client.client_id, "grant_type": grant_type}
    )
    body: dict[str, object] = {
        "access_token": issued.access_token,
        "token_type": "Bearer",
        "expires_in": issued.expires_in,
        "scope": issued.scope,
    }
    if issued.refresh_token is not None:
        body["refresh_token"] = issued.refresh_token
    return JSONResponse(body, headers=_NO_STORE)


async def _revoke(form: dict[str, str], client: OAuthClient) -> Response:
    token = form.get("token", "")
    if not token:
        raise TokenError("invalid_request", "token is required.")
    await tokens.revoke_token(token[:256], client.client_id)
    return Response(status_code=200, headers=_NO_STORE)


def _limited(wait: int) -> Response:
    return _error_response(
        TokenError(
            "temporarily_unavailable",
            "Too many failed requests. Try again later.",
            429,
            retry_after=wait,
        )
    )


async def _serve(
    request: Request, work: Callable[[dict[str, str], OAuthClient], Awaitable[Response]]
) -> Response:
    limiter: TokenLimiter = request.app.state.token_limiter
    try:
        form = await _read_form(request)
        credentials = _credentials(request, form)
    except TokenError as error:
        return _error_response(error)
    address = client_address_key(request)
    if wait := limiter.reserve_address(address):
        return _limited(wait)
    held: str | None = None
    address_failed = client_failed = False
    try:
        client = await clients.get_client(credentials.client_id)
        if client is not None and client.client_secret_hash is not None:
            if wait := limiter.reserve_client(client.client_id):
                return _limited(wait)
            held = client.client_id
        try:
            authenticated = _authenticate(client, credentials)
        except TokenError:
            address_failed = client_failed = True
            raise
        return await work(form, authenticated)
    except TokenError as error:
        address_failed = address_failed or error.error in _COUNTED_ERRORS
        return _error_response(error)
    finally:
        limiter.settle(address, held, address_failed=address_failed, client_failed=client_failed)


def register_routes(app: FastAPI) -> None:
    app.state.token_limiter = TokenLimiter()

    @app.post(TOKEN_PATH, include_in_schema=False)
    async def token(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _serve(request, _issue)

    @app.post(REVOKE_PATH, include_in_schema=False)
    async def revoke(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _serve(request, _revoke)
