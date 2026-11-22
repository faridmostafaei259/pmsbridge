"""Limits on failed sign-ins, kept in memory (the application runs as one process, see ADR 0004).

Failures are counted under two independent keys: the e-mail address that was tried, and the network
address of the visitor. Either one reaching its limit locks sign-in for that key for a short time.
The address key alone is not enough, because everyone behind one router shares an address, and the
e-mail key alone would not stop one visitor from trying many accounts. Unknown e-mail addresses are
counted exactly like known ones, so the limits reveal nothing.

An attempt is counted before its password is checked: `reserve` takes a pending slot on both keys,
and the slot is later turned into a failure or given back. Pending slots count towards the limit, so
a burst of simultaneous guesses cannot all get past a limit while the first check is still running.

Visitors whose address is private, loopback or link-local arrive through the container network, and
every IPv6 visitor currently shows up as the same private IPv4 address (the Docker gateway). Those
addresses get a much higher per-address limit; the per-e-mail limit stays the same for everyone.
"""

import hashlib
import ipaddress
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from starlette.requests import Request

EMAIL_MAX_FAILURES: Final = 5
ADDRESS_MAX_FAILURES: Final = 20
SHARED_ADDRESS_MAX_FAILURES: Final = 200
WINDOW_SECONDS: Final = 15 * 60
LOCKOUT_SECONDS: Final = 5 * 60
BUSY_RETRY_SECONDS: Final = 5
MAX_TRACKED_KEYS: Final = 10_000
IPV6_PREFIX_BITS: Final = 64

_SHARED_NETWORKS: Final = tuple(
    ipaddress.ip_network(text)
    for text in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


@dataclass
class _Entry:
    failures: deque[float] = field(default_factory=deque[float])
    locked_until: float = 0.0
    pending: int = 0


class FailureLimiter:
    """Counts failures per key; a key that reaches `max_failures` inside the window is locked."""

    def __init__(
        self,
        max_failures: int,
        *,
        window: float = WINDOW_SECONDS,
        lockout: float = LOCKOUT_SECONDS,
        max_keys: int = MAX_TRACKED_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self.max_keys = max_keys
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def lock_remaining(self, key: str) -> int:
        """Seconds left of a lockout on `key`; 0 when it is not locked."""
        entry = self._entries.get(_digest(key))
        remaining = 0.0 if entry is None else entry.locked_until - self._clock()
        return math.ceil(remaining) if remaining > 0 else 0

    def retry_after(self, key: str) -> int:
        """Seconds until `key` may try again; 0 when it may try now.

        A key whose recorded failures plus pending attempts have reached the limit is refused for a
        few seconds: if the pending attempts fail, it will be locked.
        """
        locked = self.lock_remaining(key)
        if locked:
            return locked
        entry = self._entries.get(_digest(key))
        if entry is None:
            return 0
        self._prune(entry, self._clock())
        return BUSY_RETRY_SECONDS if len(entry.failures) + entry.pending >= self.max_failures else 0

    def reserve(self, key: str) -> int:
        """Take a pending slot if `key` may try. Returns 0 when taken, else the wait in seconds."""
        wait = self.retry_after(key)
        if wait == 0:
            name = _digest(key)
            entry = self._entries.pop(name, None) or _Entry()
            entry.pending += 1
            self._entries[name] = entry
            self._trim(self._clock())
        return wait

    def release(self, key: str) -> None:
        """Give a pending slot back without counting a failure."""
        entry = self._entries.get(_digest(key))
        if entry is not None and entry.pending > 0:
            entry.pending -= 1

    def record_failure(self, key: str) -> None:
        """Count a failure. It uses up a pending slot if there is one."""
        now = self._clock()
        name = _digest(key)
        entry = self._entries.pop(name, None) or _Entry()
        entry.pending = max(0, entry.pending - 1)
        self._prune(entry, now)
        entry.failures.append(now)
        if len(entry.failures) >= self.max_failures:
            entry.locked_until = now + self.lockout
            entry.failures.clear()
        self._entries[name] = entry
        self._trim(now)

    def clear(self, key: str) -> None:
        self._entries.pop(_digest(key), None)

    def _prune(self, entry: _Entry, now: float) -> None:
        while entry.failures and entry.failures[0] <= now - self.window:
            entry.failures.popleft()

    def _trim(self, now: float) -> None:
        # Bounded memory: drop the oldest key that is neither locked nor busy; others go last.
        while len(self._entries) > self.max_keys:
            for name, entry in self._entries.items():
                if entry.locked_until <= now and entry.pending == 0:
                    del self._entries[name]
                    break
            else:
                self._entries.popitem(last=False)


class LoginLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.email = FailureLimiter(EMAIL_MAX_FAILURES, clock=clock)
        self.address = FailureLimiter(ADDRESS_MAX_FAILURES, clock=clock)
        self.shared_address = FailureLimiter(SHARED_ADDRESS_MAX_FAILURES, clock=clock)

    def _for_address(self, address: str) -> FailureLimiter:
        return self.shared_address if is_shared_address(address) else self.address

    def reserve(self, email: str, address: str) -> int:
        """Take a pending slot on both keys. Returns 0 when taken, else the wait in seconds."""
        by_address = self._for_address(address)
        wait = max(self.email.retry_after(email), by_address.retry_after(address))
        if wait == 0:
            self.email.reserve(email)
            by_address.reserve(address)
        return wait

    def locked_for(self, email: str, address: str) -> int:
        """Seconds left of a lockout on either key, ignoring pending attempts."""
        return max(
            self.email.lock_remaining(email), self._for_address(address).lock_remaining(address)
        )

    def release(self, email: str, address: str) -> None:
        self.email.release(email)
        self._for_address(address).release(address)

    def record_failure(self, email: str, address: str) -> None:
        self.email.record_failure(email)
        self._for_address(address).record_failure(address)

    def record_success(self, email: str, address: str) -> None:
        """A successful sign-in resets the e-mail count; the address only gives its slot back."""
        self.email.clear(email)
        self._for_address(address).release(address)


def client_address_key(request: Request) -> str:
    """The visitor's address as a limiter key. An IPv6 address counts as its whole /64 network."""
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host or "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, IPV6_PREFIX_BITS), strict=False))
    return str(address)


def is_shared_address(key: str) -> bool:
    """True for private, loopback and link-local addresses (and networks, for IPv6 keys)."""
    try:
        network = ipaddress.ip_network(key, strict=False)
    except ValueError:
        return False
    return any(
        network.version == shared.version and network.subnet_of(shared)  # pyright: ignore[reportArgumentType]
        for shared in _SHARED_NETWORKS
    )


def _digest(key: str) -> str:
    # Keys are hashed so that e-mail addresses are not kept in memory as plain text.
    return hashlib.sha256(key.encode()).hexdigest()[:32]
