import logging
import time
from collections.abc import Iterator
from urllib.parse import quote

import httpx
import pytest
import respx

from pmsbridge.core.logging import configure_logging
from pmsbridge.core.settings import Settings
from pmsbridge.pms.base import (
    PMSAuthError,
    PMSError,
    PMSNotFoundError,
    PMSPermissionError,
    PMSRateLimitedError,
    PMSUpstreamError,
    PMSValidationError,
    Provider,
    call_deadline,
)
from pmsbridge.pms.http import (
    BASE_URLS,
    MAX_ATTEMPTS,
    MAX_RESPONSE_BYTES,
    CircuitBreaker,
    PMSHttpClient,
    RateLimits,
    Throttle,
    get_http_client,
    parse_retry_after,
)

BASE = BASE_URLS[Provider.GUESTY]
URL = f"{BASE}/listings"
BODY_MARKER = "RAW-BODY-MUST-NOT-LEAK"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(clock: Clock) -> PMSHttpClient:
    return PMSHttpClient(
        Provider.GUESTY,
        BASE,
        sleep=clock.sleep,
        jitter=lambda: 0.5,
        breaker=CircuitBreaker(clock=clock),
    )


@pytest.fixture
def deadline_reset() -> Iterator[None]:
    yield
    call_deadline.set(None)


@pytest.fixture
def api(mock_guesty: respx.MockRouter) -> respx.MockRouter:
    return mock_guesty


# ---- retries for reads ----


async def test_a_read_is_retried_on_503_with_backoff(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    route = api.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})]
    )
    response = await client.request("GET", "/listings")
    assert response.json() == {"ok": 1}
    assert route.call_count == 3
    assert clock.slept == [0.5, 1.0]  # full jitter at 0.5 of 1 s, then of 2 s


async def test_a_read_gives_up_after_the_attempt_limit(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(503, text=BODY_MARKER))
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("GET", "/listings")
    assert route.call_count == MAX_ATTEMPTS
    assert BODY_MARKER not in error.value.message


async def test_a_read_is_retried_after_connection_errors_and_timeouts(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.get(URL).mock(
        side_effect=[httpx.ConnectError("x"), httpx.ReadTimeout("y"), httpx.Response(200, json=[])]
    )
    assert (await client.request("GET", "/listings")).status_code == 200
    assert route.call_count == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
async def test_other_statuses_are_not_retried(
    api: respx.MockRouter, client: PMSHttpClient, status: int
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(PMSError):
        await client.request("GET", "/listings")
    assert route.call_count == 1


async def test_a_429_on_a_read_honours_retry_after(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    route = api.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200)]
    )
    await client.request("GET", "/listings")
    assert clock.slept == [3.0]
    assert route.call_count == 2


# ---- writes ----


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_write_is_never_retried_on_server_errors(
    api: respx.MockRouter, client: PMSHttpClient, status: int
) -> None:
    route = api.post(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("POST", "/listings", json_body={"a": 1})
    assert route.call_count == 1
    assert "may or may not have been applied" in error.value.message


async def test_a_write_timeout_says_the_outcome_is_unknown(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.put(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("PUT", "/listings", json_body={})
    assert route.call_count == 1
    assert "outcome is unknown" in error.value.message


async def test_a_write_that_never_connected_says_nothing_was_sent(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("POST", "/listings", json_body={})
    assert route.call_count == 1
    assert "Nothing was sent" in error.value.message


async def test_a_write_is_retried_only_after_a_429_with_retry_after(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    route = api.post(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(201)]
    )
    assert (await client.request("POST", "/listings", json_body={})).status_code == 201
    assert clock.slept == [2.0]
    assert route.call_count == 2


async def test_a_write_429_without_retry_after_is_not_retried(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.post(URL).mock(return_value=httpx.Response(429))
    with pytest.raises(PMSRateLimitedError) as error:
        await client.request("POST", "/listings", json_body={})
    assert route.call_count == 1
    assert error.value.retry_after_seconds is None


async def test_repeated_429_on_a_write_stops_after_a_few_tries(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.post(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(PMSRateLimitedError):
        await client.request("POST", "/listings", json_body={})
    assert route.call_count == 3


# ---- deadlines ----


async def test_retry_after_beyond_the_deadline_fails_instead_of_waiting(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock, deadline_reset: None
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "60"}))
    call_deadline.set(time.monotonic() + 10)
    with pytest.raises(PMSRateLimitedError) as error:
        await client.request("GET", "/listings")
    assert error.value.retry_after_seconds == 60
    assert "60 seconds" in error.value.message
    assert route.call_count == 1 and clock.slept == []


async def test_a_long_retry_after_without_a_deadline_is_also_not_waited_for(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    api.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "600"}))
    with pytest.raises(PMSRateLimitedError):
        await client.request("GET", "/listings")
    assert clock.slept == []


async def test_backoff_never_goes_past_the_deadline(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock, deadline_reset: None
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(503))
    call_deadline.set(time.monotonic() + 0.2)
    with pytest.raises(PMSUpstreamError):
        await client.request("GET", "/listings")
    assert route.call_count == 1 and clock.slept == []


async def test_an_expired_deadline_sends_nothing(
    api: respx.MockRouter, client: PMSHttpClient, deadline_reset: None
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(200))
    call_deadline.set(time.monotonic() - 1)
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("GET", "/listings")
    assert "ran out" in error.value.message
    assert route.call_count == 0


async def test_timeouts_are_capped_by_the_deadline(
    api: respx.MockRouter, client: PMSHttpClient, deadline_reset: None
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(200))
    await client.request("GET", "/listings")
    default = route.calls.last.request.extensions["timeout"]
    assert default["connect"] == 5.0 and default["read"] == 20.0
    call_deadline.set(time.monotonic() + 3)
    await client.request("GET", "/listings")
    capped = route.calls.last.request.extensions["timeout"]
    assert capped["read"] <= 3.0 and capped["connect"] <= 3.0


# ---- circuit breaker ----


async def test_the_circuit_opens_after_five_failures_and_closes_after_a_probe(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(500))
    for _ in range(5):
        with pytest.raises(PMSUpstreamError):
            await client.request("GET", "/listings")
    assert route.call_count == 5
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("GET", "/listings")
    assert "paused" in error.value.message and route.call_count == 5
    clock.now += 31
    route.mock(return_value=httpx.Response(200))
    await client.request("GET", "/listings")  # the probe
    await client.request("GET", "/listings")
    assert route.call_count == 7


async def test_a_failed_probe_reopens_the_circuit(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(500))
    for _ in range(5):
        with pytest.raises(PMSUpstreamError):
            await client.request("GET", "/listings")
    clock.now += 31
    with pytest.raises(PMSUpstreamError):
        await client.request("GET", "/listings")  # the probe fails
    assert route.call_count == 6
    with pytest.raises(PMSUpstreamError):
        await client.request("GET", "/listings")
    assert route.call_count == 6


async def test_client_errors_and_rate_limits_do_not_open_the_circuit(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(404))
    for _ in range(8):
        with pytest.raises(PMSNotFoundError):
            await client.request("GET", "/listings")
    assert route.call_count == 8


async def test_one_failure_between_successes_does_not_open_the_circuit(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    api.get(URL).mock(
        side_effect=[httpx.Response(500)] * 4 + [httpx.Response(200)] + [httpx.Response(500)] * 4
    )
    for index in range(9):
        if index == 4:
            await client.request("GET", "/listings")
        else:
            with pytest.raises(PMSUpstreamError):
                await client.request("GET", "/listings")


# ---- errors and logs ----


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, PMSValidationError),
        (422, PMSValidationError),
        (401, PMSAuthError),
        (403, PMSPermissionError),
        (404, PMSNotFoundError),
        (429, PMSRateLimitedError),
        (500, PMSUpstreamError),
        (418, PMSUpstreamError),
        (302, PMSUpstreamError),
    ],
)
async def test_errors_are_mapped_and_never_include_the_body(
    api: respx.MockRouter,
    client: PMSHttpClient,
    caplog: pytest.LogCaptureFixture,
    status: int,
    error_type: type[PMSError],
) -> None:
    caplog.set_level(logging.DEBUG)
    api.post(URL).mock(
        return_value=httpx.Response(
            status, text=BODY_MARKER, headers={"x-request-id": "req-123", "location": "/elsewhere"}
        )
    )
    with pytest.raises(error_type) as error:
        await client.request("POST", "/listings", json_body={})
    assert BODY_MARKER not in error.value.message
    assert BODY_MARKER not in "\n".join(str(r.__dict__) for r in caplog.records)
    if status != 429:
        assert any(getattr(r, "pms_request_id", None) == "req-123" for r in caplog.records)


async def test_passed_through_statuses_are_returned(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    api.get(URL).mock(return_value=httpx.Response(403, text="nope"))
    response = await client.request("GET", "/listings", passthrough=frozenset({403}))
    assert response.status_code == 403
    with pytest.raises(PMSPermissionError):
        client.raise_for_status("GET", response)


async def test_an_unreadable_body_is_a_safe_error(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    api.get(URL).mock(return_value=httpx.Response(200, text=f"<html>{BODY_MARKER}"))
    response = await client.request("GET", "/listings")
    with pytest.raises(PMSUpstreamError) as error:
        response.json()
    assert BODY_MARKER not in error.value.message


async def test_a_huge_response_is_refused(api: respx.MockRouter, client: PMSHttpClient) -> None:
    api.get(URL).mock(return_value=httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("GET", "/listings")
    assert "too large" in error.value.message


async def test_requests_carry_only_what_the_caller_sets(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.get(URL).mock(return_value=httpx.Response(200))
    await client.request("GET", "/listings", params={"limit": 5}, headers={"X-Test": "1"})
    request = route.calls.last.request
    assert request.url.params["limit"] == "5" and request.headers["x-test"] == "1"
    assert request.headers["accept"] == "application/json"


# ---- constants and helpers ----


def test_base_urls_are_fixed_constants() -> None:
    assert BASE == "https://open-api.guesty.com/v1"
    with pytest.raises(PMSValidationError):
        get_http_client(Provider.HOSTAWAY)
    assert get_http_client(Provider.GUESTY) is get_http_client(Provider.GUESTY)


@pytest.mark.parametrize(
    ("value", "expected"), [("3", 3.0), ("0", 0.0), ("1.5", 1.5), (None, None), ("soon", None)]
)
def test_retry_after_seconds(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_retry_after_http_date() -> None:
    far = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert far is not None and far > 1000
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


# ---- path validation ----


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/x",
        "//evil.example/x",
        "listings",
        "/listings/../admin",
        "/listings/..",
        "/listings/./x",
        "/listings/%2e%2e/admin",
        "/listings/%2E%2E/admin",
        "/listings/%2e%2E",
        "/listings/%252e%252e/admin",
        "/listings/..%2fadmin",
        "/listings?limit=5",
        "/listings#top",
        "/listings\\x",
        "/listings/%5c..%5cx",
        "/listings/a b",
        "/listings/a\nb",
        "/x://y",
    ],
)
async def test_bad_paths_are_refused_before_sending(
    api: respx.MockRouter, client: PMSHttpClient, path: str
) -> None:
    route = api.route().mock(return_value=httpx.Response(200))
    with pytest.raises(PMSValidationError):
        await client.request("GET", path)
    assert route.call_count == 0


async def test_quoted_ids_are_accepted(api: respx.MockRouter, client: PMSHttpClient) -> None:
    route = api.get(f"{URL}/a%2Fb%3Fc").mock(return_value=httpx.Response(200))
    await client.request("GET", f"/listings/{quote('a/b?c', safe='')}")
    assert route.call_count == 1


# ---- reads stop at the attempt limit ----


async def test_reads_never_exceed_the_attempt_limit_even_with_retry_after(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    route = api.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200),
        ]
    )
    with pytest.raises(PMSRateLimitedError):
        await client.request("GET", "/listings")
    assert route.call_count == MAX_ATTEMPTS


async def test_a_huge_write_response_says_the_outcome_is_unknown(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    api.post(URL).mock(return_value=httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(PMSUpstreamError) as error:
        await client.request("POST", "/listings", json_body={})
    assert "outcome is unknown" in error.value.message


async def test_other_http_errors_become_failures_not_crashes(
    api: respx.MockRouter, client: PMSHttpClient
) -> None:
    api.get(URL).mock(side_effect=httpx.DecodingError("bad encoding"))
    with pytest.raises(PMSUpstreamError):
        await client.request("GET", "/listings")


# ---- throttle wiring ----


async def test_rate_limit_headers_slow_the_next_request(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    throttle = Throttle(RateLimits(15, 120, 5000, 8), clock=clock, sleep=clock.sleep)
    api.get(URL).mock(
        side_effect=[
            httpx.Response(200, headers={"x-ratelimit-remaining-second": "0"}),
            httpx.Response(200),
        ]
    )
    await client.request("GET", "/listings", throttle=throttle)
    assert clock.slept == []
    await client.request("GET", "/listings", throttle=throttle)
    assert len(clock.slept) == 1 and clock.slept[0] > 0


async def test_a_429_with_retry_after_holds_the_account(
    api: respx.MockRouter, client: PMSHttpClient, clock: Clock
) -> None:
    throttle = Throttle(RateLimits(15, 120, 5000, 8), clock=clock, sleep=clock.sleep)
    api.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "4"}), httpx.Response(200)]
    )
    await client.request("GET", "/listings", throttle=throttle)
    assert clock.slept == [4.0]  # the retry waits once; the hold is not waited for twice
    assert throttle._hold_until == pytest.approx(1004.0)


async def test_hold_makes_other_callers_wait(clock: Clock) -> None:
    throttle = Throttle(RateLimits(15, 120, 5000, 8), clock=clock, sleep=clock.sleep)
    throttle.hold(7.0)
    async with throttle.slot():
        pass
    assert clock.slept == [7.0]


# ---- logging ----


async def test_httpx_does_not_log_query_strings(
    api: respx.MockRouter, client: PMSHttpClient, capsys: pytest.CaptureFixture[str]
) -> None:
    api.get(URL).mock(return_value=httpx.Response(200))
    try:
        configure_logging(Settings(environment="development", log_level="INFO"))
        await client.request("GET", "/listings", params={"search": "private-guest-name"})
    finally:
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.NOTSET)
    output = capsys.readouterr().out
    assert "private-guest-name" not in output and "HTTP Request" not in output
