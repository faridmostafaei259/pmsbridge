"""A fake Guesty: the token endpoint with its 5-per-24-hours budget, and helpers for tests.

Nothing here talks to the real service. Access tokens are JWTs built at runtime from invented
values.
"""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import respx

from pmsbridge.pms.guesty.auth import TOKEN_URL

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guesty" / "auth"
DAY_SECONDS = 86400


def fixture_json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def make_jwt(cid: str, account_id: str, expires_in: int = DAY_SECONDS) -> str:
    claims = {
        "cid": cid,
        "accountId": account_id,
        "exp": int((datetime.now(UTC) + timedelta(seconds=expires_in)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64(claims)}.fake-signature"


class FakeGuestyAuth:
    """Serves `/oauth2/token`. Counts requests and enforces 5 tokens per 24 hours."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.accounts: dict[str, tuple[str, str]] = {}  # client id -> (secret, account id)
        self.issued: dict[str, list[datetime]] = {}
        self.requests = 0
        self.limit = 5
        self.exhausted_status = 429
        self.override_cid: str | None = None  # make the token claim another client id
        self.override_account: str | None = None
        self.mode: str | None = None  # "network", "garbage", "server_error"
        self.token_lifetime = DAY_SECONDS
        self.route = router.post(TOKEN_URL).mock(side_effect=self._handle)

    def add_account(self, client_id: str, secret: str, account_id: str) -> None:
        self.accounts[client_id] = (secret, account_id)

    def used(self, client_id: str) -> int:
        return len(self.issued.get(client_id, []))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.mode == "network":
            raise httpx.ConnectError("no route to host")
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        assert form["grant_type"] == "client_credentials"
        assert form["scope"] == "open-api"
        assert request.headers["accept"] == "application/json"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        client_id = form["client_id"]
        known = self.accounts.get(client_id)
        if known is None or known[0] != form["client_secret"]:
            return httpx.Response(401, json=fixture_json("invalid_client.json"))
        if self.mode == "server_error":
            return httpx.Response(500, json={"message": "boom"})
        now = datetime.now(UTC)
        history = [t for t in self.issued.get(client_id, []) if now - t < timedelta(days=1)]
        self.issued[client_id] = history
        if len(history) >= self.limit:
            wait = int((history[0] + timedelta(days=1) - now).total_seconds()) if history else 3600
            return httpx.Response(
                self.exhausted_status,
                json=fixture_json("token_budget_exhausted.json"),
                headers=self._headers(0, wait),
            )
        history.append(now)
        remaining = self.limit - len(history)
        reset = int((history[0] + timedelta(days=1) - now).total_seconds())
        if self.mode == "garbage":
            return httpx.Response(200, json={"access_token": "not-a-jwt", "expires_in": 100})
        token = make_jwt(
            self.override_cid or client_id,
            self.override_account or known[1],
            self.token_lifetime,
        )
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": self.token_lifetime,
                "access_token": token,
                "scope": "open-api",
            },
            headers=self._headers(remaining, reset),
        )

    def _headers(self, remaining: int, reset: int) -> dict[str, str]:
        return {
            "ratelimit-limit": str(self.limit),
            "ratelimit-remaining": str(remaining),
            "ratelimit-reset": str(reset),
            "x-ratelimit-remaining-day": str(remaining),
            "x-ratelimit-limit-day": str(self.limit),
        }
