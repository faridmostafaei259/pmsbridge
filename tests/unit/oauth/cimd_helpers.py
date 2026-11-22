"""A fake internet for the metadata document tests: no test ever reaches a real network.

`FakeInternet` gives each host a fake public address, answers name lookups from that table and
serves a canned HTTP response on connection. It records what the code under test did: which
addresses were connected, which server name TLS was started with, and the bytes of each request.
"""
# ruff: noqa: ASYNC109  (the network stream interface of httpcore has `timeout` parameters)

import asyncio
import json
import ssl
from collections.abc import Iterable

import httpcore

from pmsbridge.oauth.metadata_fetch import MetadataFetcher

CLIENT_URL = "https://assistant.example.test/client.json"
CALLBACK = "https://assistant.example.test/callback"


def document(url: str = CLIENT_URL, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "client_id": url,
        "client_name": "Example Assistant",
        "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "logo_uri": "https://assistant.example.test/logo.png",
        "x_unknown_field": {"ignored": True},
        **overrides,
    }
    return {name: value for name, value in body.items() if value is not None}


def http_response(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content_length: bool = True,
) -> bytes:
    sent = {"Content-Type": "application/json", "Connection": "close", **(headers or {})}
    if content_length and "Content-Length" not in sent:
        sent["Content-Length"] = str(len(body))
    lines = "".join(f"{name}: {value}\r\n" for name, value in sent.items())
    return f"HTTP/1.1 {status} Reply\r\n{lines}\r\n".encode("latin-1") + body


def json_response(content: object, **kwargs: object) -> bytes:
    return http_response(json.dumps(content).encode(), **kwargs)  # type: ignore[arg-type]


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, owner: "FakeInternet", address: str, chunks: list[bytes]) -> None:
        self._owner = owner
        self._address = address
        self._chunks = chunks

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._owner.delay:
            await asyncio.sleep(self._owner.delay)
        self._owner.unread[self._address] = len(self._chunks)
        return self._chunks.pop(0) if self._chunks else b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._owner.requests.append(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if self._owner.tls_error:
            raise httpcore.ConnectError("certificate verify failed")
        self._owner.tls.append((server_hostname, ssl_context))
        return self

    def get_extra_info(self, info: str) -> object:
        return None


class FakeInternet(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.addresses: dict[str, str] = {}  # host -> fake address
        self.replies: dict[str, bytes] = {}  # address -> canned response
        self.connects: list[str] = []
        self.requests: list[bytes] = []
        self.tls: list[tuple[str | None, ssl.SSLContext]] = []
        self.unread: dict[str, int] = {}
        self.lookups: list[str] = []
        self.delay = 0.0
        self.tls_error = False
        self.broken: set[str] = set()  # addresses that refuse connections

    def serve(self, host: str, reply: bytes, address: str | None = None) -> str:
        """Make `host` resolve to a (public) address that answers with `reply`."""
        chosen = address or self.addresses.get(host) or f"8.8.4.{len(self.addresses) + 1}"
        self.addresses[host] = chosen
        self.replies[chosen] = reply
        return chosen

    def serve_document(self, url: str = CLIENT_URL, **kwargs: object) -> None:
        host = url.split("/")[2].split(":", maxsplit=1)[0]
        self.serve(host, json_response(document(url, **kwargs)))

    async def resolve(self, host: str) -> list[str]:
        self.lookups.append(host)
        return [self.addresses[host]] if host in self.addresses else []

    def fetcher(self, **kwargs: float) -> MetadataFetcher:
        return MetadataFetcher(resolver=self.resolve, backend=self, **kwargs)

    def connects_to(self, host: str) -> int:
        return self.connects.count(self.addresses[host])

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[object] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append(host)
        if host in self.broken or host not in self.replies:
            raise httpcore.ConnectError("refused")
        reply = self.replies[host]
        step = 8192
        return FakeStream(self, host, [reply[i : i + step] for i in range(0, len(reply), step)])

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[object] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("not available")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
