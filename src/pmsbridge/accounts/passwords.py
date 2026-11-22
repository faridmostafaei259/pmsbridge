"""Password hashing with argon2id.

Hashing is deliberately slow and memory hungry, so it runs on a small thread pool: the event loop
stays free, and a burst of sign-in attempts can use at most `HASH_WORKERS` hashes at a time.
"""

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 256
HASH_WORKERS: Final = 4

# argon2-cffi's defaults follow the current RFC 9106 recommendation for argon2id.
_hasher = PasswordHasher()
_dummy_hash: str | None = None
_pool = ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="argon2")


class PasswordPolicyError(ValueError):
    """The password does not meet the rules. The message is safe to show to the person."""


def check_password_policy(password: str) -> None:
    """Length is the only rule: 12 to 256 characters, with no required character types."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Use at most {MAX_PASSWORD_LENGTH} characters.")


async def _hash(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_pool, _hasher.hash, password)


async def hash_password(password: str) -> str:
    """Hash a new password. Raises `PasswordPolicyError` when it is too short or too long."""
    check_password_policy(password)
    return await _hash(password)


async def rehash_password(password: str) -> str:
    """Hash a password that was just verified, with the current parameters and no policy check."""
    return await _hash(password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(password_hash: str, password: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(_pool, _verify, password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash was made with weaker parameters than the current ones."""
    return _hasher.check_needs_rehash(password_hash)


def prepare_dummy_hash() -> None:
    """Make the dummy hash now (at startup), so the first unknown e-mail is not slower."""
    global _dummy_hash  # noqa: PLW0603
    if _dummy_hash is None:
        _dummy_hash = _pool.submit(_hasher.hash, secrets.token_urlsafe(24)).result()


async def verify_unknown_user(password: str) -> None:
    """Spend the time of a real check, so an unknown e-mail takes as long as a wrong password."""
    global _dummy_hash  # noqa: PLW0603
    if _dummy_hash is None:
        _dummy_hash = await _hash(secrets.token_urlsafe(24))
    await verify_password(_dummy_hash, password)
