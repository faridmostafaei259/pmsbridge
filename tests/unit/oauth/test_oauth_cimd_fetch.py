"""The metadata document fetch: address rules, the guarded request, pinning and limits."""

import asyncio
import logging
import socket
import ssl
import time

import httpcore
import pytest
from cimd_helpers import CLIENT_URL, FakeInternet, http_response, json_response

from pmsbridge.oauth.metadata_fetch import (
    MAX_BODY_BYTES,
    MAX_URL_LENGTH,
    MetadataFetcher,
    MetadataFetchError,
    PinnedBackend,
    is_public_address,
    parse_max_age,
    system_resolver,
    validate_client_id_url,
)

HOST = "assistant.example.test"
URL = CLIENT_URL


# ---- The client id address ----


@pytest.mark.parametrize(
    "url",
    [
        "https://assistant.example.test/client.json",
        "https://assistant.example.test:443/client.json",
        "https://a.b.example.test/x/y/z",
        "https://xn--bcher-kva.example/metadata",
        "https://example.xn--p1ai/metadata",
        "https://example.test/a//b",
        "https://example.test/" + "a" * 100,
    ],
)
def test_valid_client_id_addresses_are_accepted(url: str) -> None:
    assert validate_client_id_url(url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://assistant.example.test/client.json",
        "ftp://assistant.example.test/client.json",
        "assistant.example.test/client.json",
        "https://1.2.3.4/client.json",
        "https://[::1]/client.json",
        "https://[2606:4700::1111]/client.json",
        "https://127.1/client.json",
        "https://0x7f.0x1/client.json",
        "https://2130706433/client.json",
        "https://localhost/client.json",
        "https://intranet/client.json",
        "https://assistant.example.test./client.json",
        "https://assistant.example.test:8443/client.json",
        "https://assistant.example.test:80/client.json",
        "https://assistant.example.test:/client.json",
        "https://assistant.example.test:0/client.json",
        "https://assistant.example.test",
        "https://assistant.example.test/",
        "https://assistant.example.test/client.json#frag",
        "https://assistant.example.test/client.json?x=1",
        "https://assistant.example.test/client.json;x=1",
        "https://assistant.example.test/a;b/client.json",
        "https://user@assistant.example.test/client.json",
        "https://user:pw@assistant.example.test/client.json",
        "https://assistant.example.test@evil.example/client.json",
        "https://assistant.example.test/a/../client.json",
        "https://assistant.example.test/./client.json",
        "https://assistant.example.test/%2e%2e/client.json",
        "https://assistant.example.test/%2E/client.json",
        "https://Assistant.Example.test/client.json",
        "https://assistant.example.test/client json",
        "https://assistant.example.test/client\\json",
        "https://assistant.example.test/clienté",
        "https://bücher.example/metadata",
        "https://assistant.example.test/\x00",
        "https://assistant.example.test/\n",
        "https://-bad.example.test/x",
        "https://exa mple.test/x",
        "https://assistant.example.test/" + "a" * 1024,
        "",
    ],
)
def test_other_client_id_addresses_are_refused(url: str) -> None:
    with pytest.raises(MetadataFetchError):
        validate_client_id_url(url)


def test_the_longest_allowed_address_is_accepted_and_one_more_character_is_not() -> None:
    prefix = "https://assistant.example.test/"
    url = prefix + "a" * (MAX_URL_LENGTH - len(prefix))
    assert len(url) == MAX_URL_LENGTH == 1024
    assert validate_client_id_url(url).host == HOST
    with pytest.raises(MetadataFetchError, match="too long"):
        validate_client_id_url(url + "a")


# ---- Which addresses may be contacted ----


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        # unspecified, this network
        "0.0.0.0",  # noqa: S104  (an address that must be refused)
        "0.1.2.3",
        # private
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.0.1",
        "192.168.255.255",
        # carrier-grade NAT
        "100.64.0.1",
        "100.127.255.255",
        # loopback
        "127.0.0.1",
        "127.255.255.254",
        # link-local and the cloud metadata address
        "169.254.0.1",
        "169.254.169.254",
        # reserved, documentation, benchmarking, protocol assignments
        "192.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "198.19.255.255",
        "198.51.100.1",
        "203.0.113.1",
        "240.0.0.1",
        "255.255.255.255",
        # multicast
        "224.0.0.1",
        "239.255.255.255",
        # IPv6: unspecified, loopback, link-local, unique local, site-local, multicast
        "::",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "fec0::1",
        "ff02::1",
        # IPv6 cloud metadata address
        "fd00:ec2::254",
        # IPv4 carried in IPv6 (private, loopback, link-local, metadata, and even public)
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:192.168.1.1",
        "::ffff:100.64.0.1",
        "::ffff:169.254.169.254",
        "::ffff:7f00:1",
        "::ffff:8.8.8.8",
        "::7f00:1",
        "64:ff9b::7f00:1",
        "64:ff9b::808:808",
        "2002:7f00:1::1",
        "2002:808:808::1",
        # other special ranges
        "2001:db8::1",
        "2001::1",
        "100::1",
        # outside global unicast space (2000::/3), and documentation space inside it
        "::ffff:0:7f00:1",
        "3fff::1",
        "3fff:fff::1",
        "5f00::1",
        "4000::1",
        "8000::1",
        "e000::1",
        # not an address at all
        "example.com",
        "",
        "256.1.1.1",
    ],
)
def test_every_other_address_class_is_refused(address: str) -> None:
    assert not is_public_address(address)


@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.5"],
        ["8.8.8.8", "10.0.0.5"],  # one private answer among public ones is enough to refuse
        ["2606:4700:4700::1111", "::1"],
        ["169.254.169.254"],
        ["fd00:ec2::254"],
        ["::ffff:127.0.0.1"],
        [],
        ["8.8.8.8"] * 8 + ["10.0.0.5"],  # a private answer past the first eight still counts
        ["2606:4700:4700::1111"] * 9 + ["fd00:ec2::254"],
    ],
)
async def test_a_host_with_any_non_public_answer_is_refused_before_connecting(
    internet: FakeInternet, answers: list[str]
) -> None:
    async def resolve(host: str) -> list[str]:
        return answers

    fetcher = MetadataFetcher(resolver=resolve, backend=internet)
    with pytest.raises(MetadataFetchError, match="does not contact"):
        await fetcher.fetch(validate_client_id_url(URL))
    assert internet.connects == []


async def test_a_failed_name_lookup_is_a_clean_failure_and_nothing_is_contacted(
    internet: FakeInternet,
) -> None:
    async def not_found(host: str) -> list[str]:
        raise MetadataFetchError("The assistant's web address could not be found.", reason="dns")

    fetcher = MetadataFetcher(resolver=not_found, backend=internet)
    with pytest.raises(MetadataFetchError, match="could not be found"):
        await fetcher.fetch(validate_client_id_url("https://nonexistent.example.test/client.json"))
    assert internet.connects == []


async def test_the_system_resolver_reports_failure_as_a_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)  # no real DNS query is ever made
    with pytest.raises(MetadataFetchError) as caught:
        await system_resolver("nonexistent.example.test")
    assert caught.value.reason == "dns"


async def test_the_system_resolver_returns_addresses_with_ipv4_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: answers)
    assert await system_resolver("example.test") == ["8.8.8.8", "2606:4700:4700::1111"]


async def test_a_resolver_that_never_answers_fails_within_the_total_timeout(
    internet: FakeInternet,
) -> None:
    async def hanging(host: str) -> list[str]:
        await asyncio.sleep(30)  # a name server that never answers
        return ["8.8.8.8"]

    fetcher = MetadataFetcher(resolver=hanging, backend=internet, total_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(MetadataFetchError, match="did not answer in time"):
        await fetcher.fetch(validate_client_id_url(URL))
    assert time.monotonic() - started < 1.0
    assert internet.connects == []


# ---- Pinning: the connection goes to the vetted address only ----


async def test_the_connection_goes_to_the_vetted_address_with_the_original_host_name(
    internet: FakeInternet,
) -> None:
    address = internet.serve(HOST, json_response({"client_id": URL}))
    fetched = await internet.fetcher().fetch(validate_client_id_url(URL))
    assert fetched.body
    assert internet.connects == [address]
    assert [name for name, _ in internet.tls] == [HOST]  # SNI and certificate name
    context = internet.tls[0][1]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    request = internet.requests[0].decode("latin-1")
    assert request.startswith("GET /client.json HTTP/1.1\r\n")
    assert f"Host: {HOST}\r\n" in request


async def test_an_explicit_port_443_is_used_and_the_host_header_has_no_port(
    internet: FakeInternet,
) -> None:
    address = internet.serve(HOST, json_response({"client_id": URL}))
    await internet.fetcher().fetch(
        validate_client_id_url("https://assistant.example.test:443/c.json")
    )
    assert internet.connects == [address]
    assert f"Host: {HOST}\r\n" in internet.requests[0].decode("latin-1")


async def test_a_second_dns_answer_cannot_swap_the_address(internet: FakeInternet) -> None:
    vetted = internet.serve(HOST, json_response({"client_id": URL}))
    internet.serve("rebind.example.test", json_response({}), address="10.0.0.9")
    answers = iter([[vetted], ["10.0.0.9"], ["10.0.0.9"]])
    lookups: list[str] = []

    async def rebinding(host: str) -> list[str]:
        lookups.append(host)
        return next(answers)

    await MetadataFetcher(resolver=rebinding, backend=internet).fetch(validate_client_id_url(URL))
    assert lookups == [HOST]  # resolved once
    assert internet.connects == [vetted]  # and only that address was used


async def test_the_pinned_backend_refuses_any_other_destination(internet: FakeInternet) -> None:
    backend = PinnedBackend(internet, HOST, ["8.8.8.8"])
    for host, port in (("10.0.0.1", 443), ("other.example.test", 443), (HOST, 8443)):
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp(host, port)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/var/run/docker.sock")
    assert internet.connects == []


async def test_a_later_vetted_address_is_tried_when_the_first_does_not_connect(
    internet: FakeInternet,
) -> None:
    internet.serve(HOST, json_response({"client_id": URL}), address="8.8.4.50")
    internet.broken.add("8.8.4.60")

    async def resolve(host: str) -> list[str]:
        return ["8.8.4.60", "8.8.4.50"]

    await MetadataFetcher(resolver=resolve, backend=internet).fetch(validate_client_id_url(URL))
    assert internet.connects == ["8.8.4.60", "8.8.4.50"]


async def test_connection_failures_and_tls_failures_are_clean(internet: FakeInternet) -> None:
    internet.serve(HOST, b"")
    internet.broken.add(internet.addresses[HOST])
    with pytest.raises(MetadataFetchError, match="reached securely"):
        await internet.fetcher().fetch(validate_client_id_url(URL))
    internet.broken.clear()
    internet.tls_error = True
    with pytest.raises(MetadataFetchError, match="reached securely"):
        await internet.fetcher().fetch(validate_client_id_url(URL))


# ---- The response ----


async def fetch_text(internet: FakeInternet, reply: bytes, **kwargs: float) -> bytes:
    internet.serve(HOST, reply)
    return (await internet.fetcher(**kwargs).fetch(validate_client_id_url(URL))).body


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_a_failure_and_is_not_followed(
    internet: FakeInternet, status: int
) -> None:
    reply = http_response(b"", status=status, headers={"Location": "https://10.0.0.1/secret"})
    with pytest.raises(MetadataFetchError, match="redirects"):
        await fetch_text(internet, reply)
    assert len(internet.connects) == 1


@pytest.mark.parametrize("status", [204, 400, 404, 500, 503])
async def test_only_status_200_is_accepted(internet: FakeInternet, status: int) -> None:
    with pytest.raises(MetadataFetchError, match="error"):
        await fetch_text(internet, http_response(b"{}", status=status))


@pytest.mark.parametrize("content_type", ["text/html", "text/plain", "application/xml", "", "json"])
async def test_a_response_that_is_not_json_is_refused(
    internet: FakeInternet, content_type: str
) -> None:
    reply = http_response(b"{}", headers={"Content-Type": content_type})
    with pytest.raises(MetadataFetchError, match="did not return JSON"):
        await fetch_text(internet, reply)


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "Application/JSON; charset=utf-8", "application/client-metadata+json"],
)
async def test_json_content_types_are_accepted(internet: FakeInternet, content_type: str) -> None:
    reply = http_response(b"{}", headers={"Content-Type": content_type})
    assert await fetch_text(internet, reply) == b"{}"


async def test_an_encoded_response_is_refused(internet: FakeInternet) -> None:
    reply = http_response(b"{}", headers={"Content-Encoding": "gzip"})
    with pytest.raises(MetadataFetchError, match="encoding"):
        await fetch_text(internet, reply)


async def test_a_body_at_the_limit_is_accepted_and_one_past_it_is_not(
    internet: FakeInternet,
) -> None:
    exact = b" " * (MAX_BODY_BYTES - 2) + b"{}"
    assert len(await fetch_text(internet, http_response(exact))) == MAX_BODY_BYTES
    with pytest.raises(MetadataFetchError, match="too large"):
        await fetch_text(internet, http_response(b" " + exact))


async def test_an_oversized_body_stops_the_read_early(internet: FakeInternet) -> None:
    body = b" " * (MAX_BODY_BYTES * 100)
    reply = http_response(body, content_length=False)
    with pytest.raises(MetadataFetchError, match="too large"):
        await fetch_text(internet, reply)
    assert internet.unread[internet.addresses[HOST]] > 100  # most of it was never read


async def test_a_declared_oversized_body_is_refused_without_reading_it(
    internet: FakeInternet,
) -> None:
    reply = http_response(b"x" * 100, headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
    with pytest.raises(MetadataFetchError, match="too large"):
        await fetch_text(internet, reply)


async def test_a_slow_server_times_out(internet: FakeInternet) -> None:
    internet.delay = 5.0
    with pytest.raises(MetadataFetchError, match="did not answer in time"):
        await fetch_text(internet, http_response(b"{}"), total_timeout=0.2)


async def test_the_default_limits_are_five_seconds_in_all_and_three_to_connect() -> None:
    fetcher = MetadataFetcher()
    assert (fetcher._total_timeout, fetcher._connect_timeout) == (5.0, 3.0)  # pyright: ignore[reportPrivateUsage]
    assert fetcher._max_bytes == 16 * 1024  # pyright: ignore[reportPrivateUsage]


# ---- Cache-Control ----


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("public, max-age=600", 600),
        ("max-age=0", 0),
        ("MAX-AGE=90, public", 90),
        ('max-age="120"', 120),
        ("no-store", 0),
        ("max-age=600, no-cache", 0),
        ("max-age=abc", None),
        ("max-age=-5", None),
        ("max-age=99999999999999", None),
        ("s-maxage=600", None),
    ],
)
def test_cache_control_is_read(header: str | None, expected: int | None) -> None:
    assert parse_max_age(header) == expected


# ---- Logging ----


async def test_a_fetch_logs_the_host_status_class_and_duration_but_never_the_body(
    internet: FakeInternet, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "BODY-MARKER-do-not-log"
    caplog.set_level(logging.DEBUG)
    await fetch_text(internet, http_response(f'{{"note": "{marker}"}}'.encode()))
    with pytest.raises(MetadataFetchError):
        await fetch_text(internet, http_response(marker.encode(), status=500))
    records = [r for r in caplog.records if r.getMessage() == "assistant metadata fetch"]
    assert [(r.metadata_host, r.status_class) for r in records] == [(HOST, "2xx"), (HOST, "none")]  # pyright: ignore[reportAttributeAccessIssue]
    assert all(isinstance(r.duration_ms, int) for r in records)  # pyright: ignore[reportAttributeAccessIssue]
    assert marker not in caplog.text
    assert "/client.json" not in caplog.text
