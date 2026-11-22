import pytest
from starlette.requests import Request

from pmsbridge.accounts import ratelimit, twofactor
from pmsbridge.accounts.ratelimit import FailureLimiter, LoginLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def request_from(host: str | None) -> Request:
    scope: dict[str, object] = {"type": "http", "headers": []}
    if host is not None:
        scope["client"] = (host, 1234)
    return Request(scope)


def test_a_key_is_locked_after_the_limit_and_recovers() -> None:
    clock = Clock()
    limiter = FailureLimiter(3, window=900, lockout=300, clock=clock)
    for _ in range(2):
        limiter.record_failure("a")
        assert limiter.retry_after("a") == 0
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 300
    clock.now += 299
    assert limiter.retry_after("a") == 1
    clock.now += 2
    assert limiter.retry_after("a") == 0


def test_failures_outside_the_window_do_not_add_up() -> None:
    clock = Clock()
    limiter = FailureLimiter(3, window=900, lockout=300, clock=clock)
    limiter.record_failure("a")
    limiter.record_failure("a")
    clock.now += 901
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 0


def test_keys_are_independent_and_clear_resets_one() -> None:
    limiter = FailureLimiter(1, clock=Clock())
    limiter.record_failure("a")
    assert limiter.retry_after("a") > 0
    assert limiter.retry_after("b") == 0
    limiter.clear("a")
    assert limiter.retry_after("a") == 0


def attempt_fails(limiter: LoginLimiter, email: str, address: str) -> int:
    """One full attempt that ends in a failure. Returns the wait if it was refused."""
    wait = limiter.reserve(email, address)
    if wait == 0:
        limiter.record_failure(email, address)
    return wait


def test_login_limiter_locks_by_email_alone_and_by_address_alone() -> None:
    clock = Clock()
    limiter = LoginLimiter(clock=clock)
    for number in range(ratelimit.EMAIL_MAX_FAILURES):
        assert attempt_fails(limiter, "ada@example.test", f"203.0.113.{number}") == 0
    assert limiter.reserve("ada@example.test", "203.0.113.99") > 0
    assert limiter.reserve("bob@example.test", "203.0.113.99") == 0

    limiter = LoginLimiter(clock=clock)
    for number in range(ratelimit.ADDRESS_MAX_FAILURES):
        assert attempt_fails(limiter, f"user-{number}@example.test", "203.0.113.50") == 0
    assert limiter.reserve("new@example.test", "203.0.113.50") > 0
    assert limiter.reserve("new@example.test", "203.0.113.51") == 0


def test_pending_attempts_count_towards_the_limit() -> None:
    limiter = LoginLimiter(clock=Clock())
    taken = [limiter.reserve("ada@example.test", f"203.0.113.{n}") for n in range(8)]
    assert taken[: ratelimit.EMAIL_MAX_FAILURES] == [0] * ratelimit.EMAIL_MAX_FAILURES
    assert all(wait > 0 for wait in taken[ratelimit.EMAIL_MAX_FAILURES :])


def test_a_released_slot_can_be_used_again_and_a_failure_uses_the_slot_up() -> None:
    limiter = FailureLimiter(2, clock=Clock())
    assert limiter.reserve("a") == 0
    assert limiter.reserve("a") == 0
    assert limiter.reserve("a") > 0
    limiter.release("a")
    assert limiter.reserve("a") == 0
    limiter.record_failure("a")
    limiter.record_failure("a")
    # Two failures reached the limit: locked, not merely busy.
    assert limiter.lock_remaining("a") > 0


def test_lock_remaining_ignores_pending_attempts() -> None:
    limiter = FailureLimiter(1, clock=Clock())
    assert limiter.reserve("a") == 0
    assert limiter.retry_after("a") > 0
    assert limiter.lock_remaining("a") == 0


def test_success_clears_the_email_counter_only() -> None:
    limiter = LoginLimiter(clock=Clock())
    for _ in range(ratelimit.EMAIL_MAX_FAILURES - 1):
        attempt_fails(limiter, "ada@example.test", "203.0.113.7")
    assert limiter.reserve("ada@example.test", "203.0.113.7") == 0
    limiter.record_success("ada@example.test", "203.0.113.7")
    attempt_fails(limiter, "ada@example.test", "203.0.113.7")
    assert limiter.reserve("ada@example.test", "203.0.113.7") == 0
    entry = limiter.address._entries.values()  # pyright: ignore[reportPrivateUsage]
    assert sum(len(e.failures) for e in entry) == ratelimit.EMAIL_MAX_FAILURES  # kept counting


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "172.18.0.1",
        "192.168.1.5",
        "100.64.0.9",
        "127.0.0.1",
        "169.254.1.1",
        "fd00::/64",
    ],
)
def test_private_addresses_get_a_much_higher_address_limit(address: str) -> None:
    assert ratelimit.is_shared_address(address)
    limiter = LoginLimiter(clock=Clock())
    for number in range(ratelimit.ADDRESS_MAX_FAILURES + 5):
        assert attempt_fails(limiter, f"user-{number}@example.test", address) == 0
    assert limiter.reserve("fresh@example.test", address) == 0
    for number in range(ratelimit.SHARED_ADDRESS_MAX_FAILURES):
        attempt_fails(limiter, f"more-{number}@example.test", address)
    assert limiter.reserve("another@example.test", address) > 0


@pytest.mark.parametrize("address", ["203.0.113.5", "8.8.8.8", "2001:db8::/64", "unknown"])
def test_public_addresses_keep_the_normal_limit(address: str) -> None:
    assert not ratelimit.is_shared_address(address)


def test_the_email_limit_applies_unchanged_to_private_addresses() -> None:
    limiter = LoginLimiter(clock=Clock())
    for _ in range(ratelimit.EMAIL_MAX_FAILURES):
        assert attempt_fails(limiter, "ada@example.test", "172.18.0.1") == 0
    assert limiter.reserve("ada@example.test", "172.18.0.1") > 0
    assert limiter.reserve("bob@example.test", "172.18.0.1") == 0


def test_memory_is_bounded_and_locked_keys_are_kept_longest() -> None:
    clock = Clock()
    limiter = FailureLimiter(2, max_keys=5, clock=clock)
    limiter.record_failure("locked")
    limiter.record_failure("locked")
    clock.now += 1
    for number in range(50):
        limiter.record_failure(f"noise-{number}")
    assert len(limiter._entries) <= 5  # pyright: ignore[reportPrivateUsage]
    assert limiter.retry_after("locked") > 0


def test_keys_are_not_stored_as_plain_text() -> None:
    limiter = FailureLimiter(2, clock=Clock())
    limiter.record_failure("ada@example.test")
    assert all("ada" not in name for name in limiter._entries)  # pyright: ignore[reportPrivateUsage]


def test_ipv6_visitors_are_grouped_by_their_network() -> None:
    first = ratelimit.client_address_key(request_from("2001:db8:1:2::1"))
    second = ratelimit.client_address_key(request_from("2001:db8:1:2:ffff::9"))
    other = ratelimit.client_address_key(request_from("2001:db8:1:3::1"))
    assert first == second == "2001:db8:1:2::/64"
    assert other != first


def test_ipv4_and_missing_addresses() -> None:
    assert ratelimit.client_address_key(request_from("203.0.113.7")) == "203.0.113.7"
    assert ratelimit.client_address_key(request_from("::ffff:203.0.113.7")) == "203.0.113.7"
    assert ratelimit.client_address_key(request_from(None)) == "unknown"


def pending_total(limiter: FailureLimiter) -> int:
    return sum(entry.pending for entry in limiter._entries.values())  # pyright: ignore[reportPrivateUsage]


def test_a_success_keeps_the_slots_of_other_attempts_in_flight() -> None:
    limiter = LoginLimiter(clock=Clock())
    email, address = "ada@example.test", "203.0.113.7"
    assert limiter.reserve(email, address) == 0  # attempt A
    assert limiter.reserve(email, address) == 0  # attempt B, still being checked
    limiter.record_success(email, address)  # A succeeds
    assert pending_total(limiter.email) == 1
    assert pending_total(limiter.address) == 1
    limiter.record_failure(email, address)  # B fails afterwards
    assert pending_total(limiter.email) == 0
    assert pending_total(limiter.address) == 0
    entry = limiter.email._entries[ratelimit._digest(email)]  # pyright: ignore[reportPrivateUsage]
    assert len(entry.failures) == 1  # B was still counted


def test_a_success_forgets_failures_and_lockout_but_not_pending_slots() -> None:
    clock = Clock()
    limiter = FailureLimiter(2, clock=clock)
    limiter.record_failure("a")
    assert limiter.reserve("a") == 0
    assert limiter.reserve("a") > 0  # one failure plus one pending reaches the limit
    limiter.succeed("a")
    assert limiter.lock_remaining("a") == 0
    assert limiter.retry_after("a") == 0
    assert limiter.reserve("a") == 0
    limiter.succeed("a")
    assert limiter._entries == {}  # idle keys are forgotten  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "host",
    ["::1", "fe80::1", "fe80::a:b:c:d", "febf::1", "::ffff:127.0.0.1", "::ffff:10.1.2.3"],
)
def test_ipv6_loopback_and_link_local_addresses_stay_in_the_shared_group(host: str) -> None:
    key = ratelimit.client_address_key(request_from(host))
    assert ratelimit.is_shared_address(key)
    limiter = LoginLimiter(clock=Clock())
    for number in range(ratelimit.ADDRESS_MAX_FAILURES + 5):
        assert attempt_fails(limiter, f"user-{number}@example.test", key) == 0


def test_other_ipv6_addresses_are_still_grouped_by_network() -> None:
    key = ratelimit.client_address_key(request_from("2001:db8:5::7"))
    assert key == "2001:db8:5::/64"
    assert not ratelimit.is_shared_address(key)


# ---- The two limits on wrong second-factor codes ----


def test_code_limits_five_in_fifteen_minutes_and_twenty_in_a_day() -> None:
    clock = Clock()
    limiter = twofactor.CodeLimiter(clock=clock)

    def wrong() -> int:
        wait = limiter.reserve("user:1")
        if wait == 0:
            limiter.record_failure("user:1")
        return wait

    for _ in range(3):  # three rounds of five wrong codes, each followed by the 15 minute pause
        assert [wrong() for _ in range(5)] == [0] * 5
        assert wrong() > 0
        clock.now += twofactor.CODE_WINDOW_SECONDS + 1
    # A correct code resets the short limit but not the day's total.
    assert limiter.reserve("user:1") == 0
    limiter.succeed("user:1")
    assert [wrong() for _ in range(4)] == [0] * 4
    assert limiter.daily.lock_remaining("user:1") == 0
    assert wrong() == 0  # the twentieth failure of the day
    wait = wrong()
    assert wait > twofactor.CODE_WINDOW_SECONDS  # locked for the rest of the day
    clock.now += twofactor.CODE_WINDOW_SECONDS + 1
    assert limiter.reserve("user:1") > 0
    clock.now += twofactor.DAILY_WINDOW_SECONDS
    assert limiter.reserve("user:1") == 0


def test_code_limits_are_per_person_and_count_slots_in_flight() -> None:
    limiter = twofactor.CodeLimiter(clock=Clock())
    assert [limiter.reserve("user:1") for _ in range(5)] == [0] * 5
    assert limiter.reserve("user:1") > 0  # five checks still running use up the limit
    assert limiter.reserve("user:2") == 0
    limiter.release("user:1")
    assert limiter.reserve("user:1") == 0
