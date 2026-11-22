import pytest
from argon2 import PasswordHasher, Type, extract_parameters

from conftest import REAL_HASHER
from pmsbridge.accounts import passwords


async def test_hash_is_argon2id_and_verifies() -> None:
    stored = await passwords.hash_password("a long enough passphrase")
    assert stored.startswith("$argon2id$")
    assert await passwords.verify_password(stored, "a long enough passphrase")
    assert not await passwords.verify_password(stored, "a different passphrase")


async def test_the_same_password_gets_a_different_salt() -> None:
    first = await passwords.hash_password("a long enough passphrase")
    second = await passwords.hash_password("a long enough passphrase")
    assert first != second


async def test_a_damaged_stored_hash_fails_instead_of_raising() -> None:
    assert not await passwords.verify_password("not-a-hash", "a long enough passphrase")


def test_the_applications_own_hasher_is_argon2id_with_at_least_the_library_defaults() -> None:
    library = PasswordHasher()
    stored = REAL_HASHER.hash("a long enough passphrase")
    parameters = extract_parameters(stored)
    assert stored.startswith("$argon2id$")
    assert parameters.type == Type.ID
    assert parameters.time_cost >= library.time_cost
    assert parameters.memory_cost >= library.memory_cost
    assert parameters.parallelism >= library.parallelism
    assert library.time_cost >= 3
    assert library.memory_cost >= 64 * 1024


@pytest.mark.parametrize("password", ["", "short", "elevenchars"])
async def test_too_short_passwords_are_refused(password: str) -> None:
    with pytest.raises(passwords.PasswordPolicyError, match="at least 12"):
        await passwords.hash_password(password)


async def test_length_limits_and_no_composition_rules() -> None:
    assert await passwords.hash_password("a" * passwords.MIN_PASSWORD_LENGTH)
    assert await passwords.hash_password("a" * passwords.MAX_PASSWORD_LENGTH)
    with pytest.raises(passwords.PasswordPolicyError, match="at most 256"):
        await passwords.hash_password("a" * (passwords.MAX_PASSWORD_LENGTH + 1))


async def test_a_hash_with_weaker_parameters_needs_a_rehash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await passwords.hash_password("a long enough passphrase")
    assert not passwords.needs_rehash(old)
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    )
    assert passwords.needs_rehash(old)
    upgraded = await passwords.rehash_password("a long enough passphrase")
    assert not passwords.needs_rehash(upgraded)
    assert await passwords.verify_password(upgraded, "a long enough passphrase")


async def test_unknown_user_check_runs_a_real_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Counting(PasswordHasher):
        def verify(self, hash: str | bytes, password: str | bytes) -> bool:
            calls.append(str(hash))
            return super().verify(hash, password)

    monkeypatch.setattr(passwords, "_hasher", Counting(time_cost=1, memory_cost=8, parallelism=1))
    monkeypatch.setattr(passwords, "_dummy_hash", None)
    await passwords.verify_unknown_user("whatever the visitor typed")
    await passwords.verify_unknown_user("and again")
    assert len(calls) == 2
    # The dummy hash has the same parameters as real ones, so the work is the same.
    assert calls[0].startswith("$argon2id$")
    assert not passwords.needs_rehash(calls[0])
    assert calls[0] == calls[1]
