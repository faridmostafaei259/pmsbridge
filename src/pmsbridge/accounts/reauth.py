"""Remembers which sessions confirmed their password and a code a short while ago.

Sensitive administrator actions (inviting, disabling, changing roles, resetting two-factor sign-in)
need a confirmation within the last 10 minutes. The record lives in memory, tied to the hashed
session id and dropped at sign-out: restarting the application, or a session ending, only means the
person is asked again.
"""

import time
from collections.abc import Callable
from typing import Final

from starlette.requests import Request

from pmsbridge.accounts.sessions import hash_token

REAUTH_SECONDS: Final = 10 * 60
MAX_ENTRIES: Final = 1000


class ReauthStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._confirmed: dict[str, float] = {}

    def _prune(self) -> None:
        """Drop expired entries, then the oldest until there is room for one more."""
        cutoff = self._clock() - REAUTH_SECONDS
        for key in [key for key, at in self._confirmed.items() if at <= cutoff]:
            del self._confirmed[key]
        while len(self._confirmed) > MAX_ENTRIES - 1:
            del self._confirmed[min(self._confirmed, key=lambda key: self._confirmed[key])]

    def mark(self, session_id: str) -> None:
        self._confirmed.pop(session_id, None)
        self._prune()
        self._confirmed[session_id] = self._clock()

    def is_recent(self, session_id: str) -> bool:
        at = self._confirmed.get(session_id)
        return at is not None and self._clock() - at < REAUTH_SECONDS

    def forget(self, session_id: str) -> None:
        self._confirmed.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._confirmed)


def forget_confirmation(request: Request, session_cookie: str) -> None:
    """Drop the confirmation of a session that is ending (a no-op where no store exists)."""
    store = getattr(request.app.state, "reauth", None)
    if isinstance(store, ReauthStore) and session_cookie:
        store.forget(hash_token(session_cookie))
