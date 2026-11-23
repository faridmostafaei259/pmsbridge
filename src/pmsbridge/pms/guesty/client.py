"""The Guesty API client: bearer token, per-account throttling and the single expiry retry.

Domain modules call `GuestyClient.request` with a path under `/v1` and get an `UpstreamResponse`
for a successful call, or a client-safe `PMSError`. This client never asks Guesty for a token
itself: it asks `GuestyTokenManager`, which owns the token budget.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Final

import httpx

from pmsbridge.core.settings import get_settings
from pmsbridge.pms.base import PMSAuthError, PMSPermissionError, Provider
from pmsbridge.pms.guesty.auth import GuestyTokenManager
from pmsbridge.pms.http import (
    PMSHttpClient,
    RateLimits,
    Throttle,
    UpstreamResponse,
    get_http_client,
)

logger = logging.getLogger(__name__)

# Guesty's limits per account (https://open-api-docs.guesty.com/docs/rate-limits).
PER_SECOND: Final = 15
PER_MINUTE: Final = 120
PER_HOUR: Final = 5000
EXPIRED_TOKEN_TEXT: Final = b"don't have permission to access"

# One throttle per Guesty account, shared by every provider instance for that account.
_throttles: dict[str, Throttle] = {}


def throttle_for(account_key: str) -> Throttle:
    if account_key not in _throttles:
        _throttles[account_key] = Throttle(
            RateLimits(PER_SECOND, PER_MINUTE, PER_HOUR, get_settings().pms_max_concurrency)
        )
    return _throttles[account_key]


def reset_throttles() -> None:
    """Forget every throttle (tests)."""
    _throttles.clear()


def _looks_expired(response: UpstreamResponse) -> bool:
    """Guesty's documented signs of an expired token: 401, or 403 with the permission message."""
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        return True
    return (
        response.status_code == HTTPStatus.FORBIDDEN
        and EXPIRED_TOKEN_TEXT in response.content[:2048].lower()
    )


class GuestyClient:
    def __init__(
        self, auth: GuestyTokenManager, connection_id: str, http: PMSHttpClient | None = None
    ) -> None:
        self._auth = auth
        self._http = http or get_http_client(Provider.GUESTY)
        self._connection_id = connection_id

    @property
    def account_key(self) -> str:
        return self._auth.account_id or f"connection:{self._connection_id}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: object | None = None,
    ) -> UpstreamResponse:
        token = await self._auth.get_token()
        throttle = throttle_for(self.account_key)
        response = await self._send(method, path, params, json_body, token=token, throttle=throttle)
        if httpx.codes.is_success(response.status_code):
            await self._auth.touch_success()
            return response
        if not _looks_expired(response):
            self._http.raise_for_status(method, response)
        # At most one refresh per request, and only when the stored token is due (or was already
        # replaced by someone else). Otherwise the rejection is a real permission problem.
        fresh = await self._auth.refresh_after_rejection(token)
        if fresh is None:
            raise PMSPermissionError(
                "Guesty denied access although the access token is still valid. The API key "
                "may not be allowed to do this."
            )
        if method.upper() not in ("GET", "HEAD"):
            # Only reads are sent again. A change is never resent automatically.
            raise PMSAuthError(
                "The Guesty token had expired and has been renewed. Nothing was changed. "
                "Try the request again."
            )
        retried = await self._send(method, path, params, json_body, token=fresh, throttle=throttle)
        if retried.status_code == HTTPStatus.UNAUTHORIZED:
            await self._auth.mark_needs_attention()
        self._http.raise_for_status(method, retried)
        await self._auth.touch_success()
        return retried

    async def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | int] | None,
        json_body: object | None,
        *,
        token: str,
        throttle: Throttle,
    ) -> UpstreamResponse:
        return await self._http.request(
            method,
            path,
            params=params,
            json_body=json_body,
            headers={"Authorization": f"Bearer {token}"},
            throttle=throttle,
            passthrough=frozenset({401, 403}),
        )

    async def aclose(self) -> None:
        """Nothing to close: the HTTP client is shared by every connection of the provider."""
