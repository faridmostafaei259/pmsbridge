"""Storing and checking second factors: authenticator secrets and recovery codes.

The authenticator secret is encrypted with the vault, bound to the user id. A code is accepted for
the current 30-second step and one either side, and each step is accepted once: the newest accepted
step is stored and a code from that step or an older one is refused. A recovery code works once.
Both single-use updates are done as one conditional statement inside a write transaction, so two
requests sent at the same moment cannot both use the same code.

Wrong codes are limited per person (see `CodeLimiter`) with the same pending-before-check pattern
as passwords (see `ratelimit.py`).
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pmsbridge.accounts import totp
from pmsbridge.accounts.db import RecoveryCode, UserTotp, flow_aad, totp_aad
from pmsbridge.accounts.flows import rows_changed
from pmsbridge.accounts.ratelimit import FailureLimiter
from pmsbridge.core.crypto import get_vault
from pmsbridge.core.storage import session, write_transaction

CODE_MAX_FAILURES: Final = 5
CODE_WINDOW_SECONDS: Final = 15 * 60
DAILY_MAX_FAILURES: Final = 20
DAILY_WINDOW_SECONDS: Final = 24 * 60 * 60


class CodeLimiter:
    """Two limits on wrong codes per person: 5 in 15 minutes and 20 in 24 hours.

    The short limit stops quick guessing and is reset by a correct code. The daily limit is a cap
    on the total guessing budget and is not reset: a correct code only gives its slot back.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.short = FailureLimiter(
            CODE_MAX_FAILURES, window=CODE_WINDOW_SECONDS, lockout=CODE_WINDOW_SECONDS, clock=clock
        )
        self.daily = FailureLimiter(
            DAILY_MAX_FAILURES,
            window=DAILY_WINDOW_SECONDS,
            lockout=DAILY_WINDOW_SECONDS,
            clock=clock,
        )

    def reserve(self, key: str) -> int:
        """Take a pending slot on both limits. Returns 0 when taken, else the wait in seconds."""
        wait = max(self.short.retry_after(key), self.daily.retry_after(key))
        if wait == 0:
            self.short.reserve(key)
            self.daily.reserve(key)
        return wait

    def release(self, key: str) -> None:
        self.short.release(key)
        self.daily.release(key)

    def record_failure(self, key: str) -> None:
        self.short.record_failure(key)
        self.daily.record_failure(key)

    def succeed(self, key: str) -> None:
        self.short.succeed(key)
        self.daily.release(key)


def new_code_limiter() -> CodeLimiter:
    return CodeLimiter()


def limiter_key(user_id: int) -> str:
    return f"user:{user_id}"


def seal_pending_secret(secret: bytes, flow_id: str) -> bytes:
    return get_vault().encrypt(secret, flow_aad(flow_id))


def open_pending_secret(sealed: bytes, flow_id: str) -> bytes:
    return get_vault().decrypt(sealed, flow_aad(flow_id))


async def has_totp(user_id: int) -> bool:
    async with session() as db:
        return await db.get(UserTotp, user_id) is not None


def add_totp(db: AsyncSession, user_id: int, secret: bytes, step: int) -> None:
    """Save a confirmed authenticator; `step` is the code that confirmed it (not accepted again)."""
    db.add(
        UserTotp(
            user_id=user_id,
            secret=get_vault().encrypt(secret, totp_aad(user_id)),
            last_step=step,
            created_at=datetime.now(UTC),
        )
    )


async def replace_recovery_codes(db: AsyncSession, user_id: int) -> list[str]:
    """Delete the unused codes and make new ones. Returns them: this is the only time they exist."""
    codes = [totp.new_recovery_code() for _ in range(totp.RECOVERY_CODE_COUNT)]
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    now = datetime.now(UTC)
    for code in codes:
        normalised = totp.normalise_recovery_code(code)
        assert normalised is not None  # noqa: S101  (we just made it in the right shape)
        db.add(
            RecoveryCode(
                user_id=user_id, code_hash=totp.hash_recovery_code(normalised), created_at=now
            )
        )
    return codes


async def remove_two_factor(db: AsyncSession, user_id: int) -> None:
    await db.execute(delete(UserTotp).where(UserTotp.user_id == user_id))
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))


async def remaining_recovery_codes(user_id: int) -> int:
    async with session() as db:
        rows = await db.execute(select(RecoveryCode.id).where(RecoveryCode.user_id == user_id))
        return len(rows.all())


async def _use_totp_code(user_id: int, code: str) -> bool:
    async with session() as db:
        row = await db.get(UserTotp, user_id)
    if row is None:
        return False
    secret = get_vault().decrypt(row.secret, totp_aad(user_id))
    step = totp.matching_step(secret, code)
    if step is None:
        return False
    async with write_transaction() as db:
        result = await db.execute(
            update(UserTotp)
            .where(
                and_(
                    UserTotp.user_id == user_id,
                    or_(UserTotp.last_step.is_(None), UserTotp.last_step < step),
                )
            )
            .values(last_step=step)
        )
    return rows_changed(result) == 1


async def _use_recovery_code(user_id: int, code: str) -> bool:
    normalised = totp.normalise_recovery_code(code)
    if normalised is None:
        return False
    async with write_transaction() as db:
        result = await db.execute(
            delete(RecoveryCode).where(
                RecoveryCode.user_id == user_id,
                RecoveryCode.code_hash == totp.hash_recovery_code(normalised),
            )
        )
    return rows_changed(result) == 1


async def use_code(user_id: int, text: str) -> bool:
    """Accept an authenticator code or a recovery code, once. True if it was valid and unused."""
    code = text.strip().replace(" ", "")
    if totp.is_totp_shaped(code):
        return await _use_totp_code(user_id, code)
    return await _use_recovery_code(user_id, text)
