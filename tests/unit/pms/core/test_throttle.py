import asyncio
import time

import pytest

from pmsbridge.core.settings import clear_settings_cache
from pmsbridge.pms.base import PMSRateLimitedError, call_deadline
from pmsbridge.pms.guesty.client import PER_HOUR, PER_MINUTE, PER_SECOND, throttle_for
from pmsbridge.pms.http import RateLimits, Throttle, TokenBucket


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make(limits: RateLimits, clock: Clock) -> Throttle:
    return Throttle(limits, clock=clock, sleep=clock.sleep)


async def use(throttle: Throttle, times: int) -> None:
    for _ in range(times):
        async with throttle.slot():
            pass


def test_the_bucket_refills_over_time() -> None:
    clock = Clock()
    bucket = TokenBucket(2, 1.0, clock)
    bucket.take()
    bucket.take()
    assert bucket.wait_time() == pytest.approx(0.5)
    clock.now += 0.5
    assert bucket.wait_time() == 0


async def test_guesty_limits_are_the_documented_ones() -> None:
    assert (PER_SECOND, PER_MINUTE, PER_HOUR) == (15, 120, 5000)


async def test_fifteen_per_second_then_it_waits() -> None:
    clock = Clock()
    throttle = make(RateLimits(15, 120, 5000, 8), clock)
    await use(throttle, 15)
    assert clock.slept == []
    await use(throttle, 1)
    assert len(clock.slept) == 1 and clock.slept[0] == pytest.approx(1 / 15)


async def test_the_minute_window_is_enforced_across_seconds() -> None:
    clock = Clock()
    throttle = make(RateLimits(15, 20, 5000, 8), clock)
    await use(throttle, 15)
    clock.now += 1.0  # the per-second window is open again, the minute window is not
    await use(throttle, 5)
    assert clock.slept == []
    await use(throttle, 1)
    assert clock.slept and clock.slept[0] > 1.0


async def test_the_hour_window_is_enforced() -> None:
    clock = Clock()
    throttle = make(RateLimits(1000, 1000, 5, 8), clock)
    await use(throttle, 5)
    assert clock.slept == []
    with pytest.raises(PMSRateLimitedError) as error:  # no deadline: never waits this long
        await use(throttle, 1)
    assert error.value.retry_after_seconds == pytest.approx(720.0)
    call_deadline.set(time.monotonic() + 5000)
    try:
        await use(throttle, 1)
    finally:
        call_deadline.set(None)
    assert clock.slept[0] == pytest.approx(720.0)


async def test_slowing_down_when_the_pms_reports_little_room() -> None:
    clock = Clock()
    throttle = make(RateLimits(15, 120, 5000, 8), clock)
    throttle.observe({"x-ratelimit-remaining-second": "0", "x-ratelimit-remaining-minute": "100"})
    await use(throttle, 1)
    assert clock.slept and clock.slept[0] > 0
    throttle.observe({"x-ratelimit-remaining-minute": "not a number"})  # ignored safely


async def test_a_wait_longer_than_the_deadline_is_an_error_not_a_sleep() -> None:
    clock = Clock()
    throttle = make(RateLimits(1000, 1000, 1, 8), clock)
    await use(throttle, 1)
    call_deadline.set(clock.now + 5)
    try:
        with pytest.raises(PMSRateLimitedError) as error:
            await use(throttle, 1)
    finally:
        call_deadline.set(None)
    assert error.value.retry_after_seconds == pytest.approx(3600.0)
    assert "3600 seconds" in error.value.message
    assert clock.slept == []


async def test_concurrency_is_capped() -> None:
    throttle = Throttle(RateLimits(1000, 1000, 100000, 3))
    running = 0
    peak = 0

    async def work() -> None:
        nonlocal running, peak
        async with throttle.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(work() for _ in range(12)))
    assert peak == 3


async def test_waiting_for_a_slot_stops_at_the_deadline() -> None:
    throttle = Throttle(RateLimits(1000, 1000, 100000, 1))
    async with throttle.slot():
        call_deadline.set(time.monotonic() + 0.05)
        try:
            with pytest.raises(PMSRateLimitedError):
                await use(throttle, 1)
        finally:
            call_deadline.set(None)


async def test_a_slot_is_released_when_the_work_fails() -> None:
    throttle = Throttle(RateLimits(1000, 1000, 100000, 1))
    with pytest.raises(RuntimeError):
        async with throttle.slot():
            raise RuntimeError("boom")
    await asyncio.wait_for(use(throttle, 1), timeout=1)


async def test_one_throttle_per_account_and_the_concurrency_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PMS_MAX_CONCURRENCY", "2")
    clear_settings_cache()
    first = throttle_for("acct-1")
    assert throttle_for("acct-1") is first
    assert throttle_for("acct-2") is not first
    running = 0
    peak = 0

    async def work() -> None:
        nonlocal running, peak
        async with first.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(work() for _ in range(6)))
    assert peak == 2
