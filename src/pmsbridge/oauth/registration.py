"""Dynamic Client Registration (RFC 7591) at `POST /oauth/register`.

JSON only, 16 KiB at most. Registrations are rate limited per visitor address and for the whole
installation, and the metadata is validated strictly: redirect URIs follow `redirects.py`, the
name is required and shown (escaped) on the consent page. Metadata the server does not use is
ignored. Error answers never repeat what the caller sent.

A client that does not name a `token_endpoint_auth_method` is registered as a public client
(`none`, which is what AI assistants use together with PKCE) and gets no secret. A secret is
created only when a secret method was asked for; it is shown once and stored as a hash.
"""

import json
import time
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Final

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pmsbridge.accounts.ratelimit import client_address_key, is_shared_address
from pmsbridge.oauth import clients
from pmsbridge.oauth.config import AUTH_METHODS, GRANT_TYPES, REGISTER_PATH
from pmsbridge.oauth.redirects import InvalidRedirectError, validate_redirect_uri

MAX_BODY_BYTES: Final = 16 * 1024
MAX_REDIRECT_URIS: Final = 10
MAX_NAME_LENGTH: Final = 100
PER_ADDRESS_PER_HOUR: Final = 10
GLOBAL_PER_HOUR: Final = 30
WINDOW_SECONDS: Final = 3600
MAX_TRACKED_ADDRESSES: Final = 10_000


class RegistrationError(Exception):
    """A request that cannot be registered: the OAuth error code and a fixed description."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


class RegistrationLimiter:
    """Counts registrations in a sliding hour: per address, and for the installation as a whole.

    A public address counts as one visitor (a public IPv6 address is keyed by its /64 network),
    so a company behind one public address shares one per-address allowance. Visitors that arrive
    from a private network, VPN or the container network (see `accounts/ratelimit.py`) cannot be
    told apart and are limited only by the global limit.
    """

    def __init__(
        self,
        *,
        per_address: int = PER_ADDRESS_PER_HOUR,
        global_limit: int = GLOBAL_PER_HOUR,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_address = per_address
        self._global_limit = global_limit
        self._window = window
        self._clock = clock
        self._global: deque[float] = deque()
        self._by_address: OrderedDict[str, deque[float]] = OrderedDict()

    def _wait(self, hits: deque[float], limit: int, now: float) -> int:
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) < limit:
            return 0
        return max(1, int(hits[0] + self._window - now) + 1)

    def check(self, address: str) -> int:
        """Count a registration attempt. Returns 0 if allowed, else the seconds to wait."""
        now = self._clock()
        shared = is_shared_address(address)
        hits = self._by_address.pop(address, deque[float]())
        wait = max(
            self._wait(self._global, self._global_limit, now),
            0 if shared else self._wait(hits, self._per_address, now),
        )
        if wait == 0:
            self._global.append(now)
            hits.append(now)
        if hits:
            self._by_address[address] = hits
        while len(self._by_address) > MAX_TRACKED_ADDRESSES:
            self._by_address.popitem(last=False)
        return wait


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise RegistrationError("invalid_client_metadata", "client_name is required.")
    name = value.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise RegistrationError(
            "invalid_client_metadata", f"client_name must be 1 to {MAX_NAME_LENGTH} characters."
        )
    if any(unicodedata.category(char)[0] in "CZ" and char != " " for char in name):
        raise RegistrationError(
            "invalid_client_metadata", "client_name contains characters that are not allowed."
        )
    return name


def _redirect_uris(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RegistrationError("invalid_redirect_uri", "redirect_uris must be a non-empty list.")
    items: list[object] = list(value)  # pyright: ignore[reportUnknownArgumentType]
    if len(items) > MAX_REDIRECT_URIS:
        raise RegistrationError(
            "invalid_redirect_uri", f"At most {MAX_REDIRECT_URIS} redirect_uris are allowed."
        )
    uris: list[str] = []
    for number, item in enumerate(items, start=1):
        try:
            uri = validate_redirect_uri(item)
        except InvalidRedirectError as error:
            raise RegistrationError(
                "invalid_redirect_uri", f"redirect_uris entry {number} {error}."
            ) from None
        if uri not in uris:
            uris.append(uri)
    return uris


def _choice(value: object, allowed: tuple[str, ...], field: str) -> list[str]:
    items: list[object] = list(value) if isinstance(value, list) else [value]  # pyright: ignore[reportUnknownArgumentType]
    if not items or any(not isinstance(item, str) or item not in allowed for item in items):
        raise RegistrationError(
            "invalid_client_metadata", f"{field} may only contain: {', '.join(allowed)}."
        )
    return list(dict.fromkeys(str(item) for item in items))


def parse_registration(payload: object) -> clients.RegistrationData:
    """Validate the registration metadata. Raises `RegistrationError` with a fixed message."""
    if not isinstance(payload, dict):
        raise RegistrationError("invalid_client_metadata", "The body must be a JSON object.")
    data: dict[str, object] = {str(key): item for key, item in payload.items()}  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
    name = _name(data.get("client_name"))
    uris = _redirect_uris(data.get("redirect_uris"))
    method = data.get("token_endpoint_auth_method", "none")
    if not isinstance(method, str) or method not in AUTH_METHODS:
        raise RegistrationError(
            "invalid_client_metadata",
            f"token_endpoint_auth_method must be one of: {', '.join(AUTH_METHODS)}.",
        )
    grants = _choice(data.get("grant_types", ["authorization_code"]), GRANT_TYPES, "grant_types")
    if "authorization_code" not in grants:
        raise RegistrationError(
            "invalid_client_metadata", "grant_types must include authorization_code."
        )
    if _choice(data.get("response_types", ["code"]), ("code",), "response_types") != ["code"]:
        raise RegistrationError("invalid_client_metadata", "response_types must be code.")
    return clients.RegistrationData(name, uris, method, grants)


async def _read_json(request: Request) -> object:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise RegistrationError(
            "invalid_client_metadata", "Send the registration as application/json.", 415
        )
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > MAX_BODY_BYTES:
        raise RegistrationError("invalid_client_metadata", "The request is too large.", 413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise RegistrationError("invalid_client_metadata", "The request is too large.", 413)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise RegistrationError("invalid_client_metadata", "The body is not valid JSON.") from None


def _error(error: RegistrationError, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        {"error": error.error, "error_description": error.description},
        status_code=error.status,
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def register_routes(app: FastAPI) -> None:
    app.state.registration_limiter = RegistrationLimiter()

    @app.post(REGISTER_PATH, include_in_schema=False)
    async def register_client(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        limiter: RegistrationLimiter = request.app.state.registration_limiter
        if wait := limiter.check(client_address_key(request)):
            return _error(
                RegistrationError(
                    "temporarily_unavailable",
                    "Too many registrations. Try again later.",
                    429,
                ),
                {"Retry-After": str(wait)},
            )
        try:
            data = parse_registration(await _read_json(request))
        except RegistrationError as error:
            return _error(error)
        created = await clients.create_client(data)
        body: dict[str, object] = {
            "client_id": created.client_id,
            "client_id_issued_at": int(created.created_at.timestamp()),
            "client_name": data.name,
            "redirect_uris": data.redirect_uris,
            "grant_types": data.grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": data.auth_method,
        }
        if created.client_secret is not None:
            body["client_secret"] = created.client_secret
            body["client_secret_expires_at"] = 0
        return JSONResponse(body, status_code=201, headers={"Cache-Control": "no-store"})
