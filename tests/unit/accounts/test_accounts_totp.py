import asyncio

import pytest

from conftest import PASSWORD, add_authenticator, current_code
from pmsbridge.accounts import totp, twofactor
from pmsbridge.accounts.db import RecoveryCode, UserTotp, totp_aad
from pmsbridge.accounts.service import create_user
from pmsbridge.admin import keys as admin_keys
from pmsbridge.core.crypto import Vault, VaultError, encrypted_fields, generate_key, get_vault
from pmsbridge.core.settings import get_settings, parse_encryption_keys
from pmsbridge.core.storage import session, write_transaction

FROZEN_STEP = 56_666_666  # the clock is stopped, so a test never straddles a 30 second step


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call that asks for the current step gets the same one, unless a time is passed in."""

    def step(now: float | None = None) -> int:
        return FROZEN_STEP if now is None else int(now // totp.PERIOD_SECONDS)

    monkeypatch.setattr(totp, "current_step", step)


# ---- The code algorithm ----

RFC_SECRET = b"12345678901234567890"  # the test secret from RFC 6238 appendix B


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924")],
)
def test_codes_match_the_rfc_6238_test_vectors(seconds: int, expected: str) -> None:
    assert totp.code_for(RFC_SECRET, totp.current_step(seconds)) == expected


def test_the_window_is_the_current_step_and_one_either_side() -> None:
    now = 1_700_000_000.0
    step = totp.current_step(now)
    for offset in (-1, 0, 1):
        assert totp.matching_step(RFC_SECRET, totp.code_for(RFC_SECRET, step + offset), now) == (
            step + offset
        )
    for offset in (-2, 2):
        assert totp.matching_step(RFC_SECRET, totp.code_for(RFC_SECRET, step + offset), now) is None


@pytest.mark.parametrize("text", ["", "12345", "1234567", "12345a", "١٢٣٤٥٦", " 123456", "123 456"])
def test_codes_of_the_wrong_shape_are_refused_before_any_check(text: str) -> None:
    assert totp.matching_step(RFC_SECRET, text) is None


def test_the_secret_is_160_bits_and_the_uri_and_picture_are_safe() -> None:
    secret = totp.new_secret()
    assert len(secret) * 8 == 160
    assert secret != totp.new_secret()
    uri = totp.otpauth_uri(secret, "ada+x@example.test")
    assert uri.startswith("otpauth://totp/pmsbridge%3Aada%2Bx%40example.test?")
    assert f"secret={totp.secret_text(secret)}" in uri
    assert "issuer=pmsbridge" in uri
    assert "algorithm=SHA1&digits=6&period=30" in uri
    svg = totp.qr_svg(uri)
    assert svg.startswith("<svg")
    assert "<script" not in svg
    assert " style=" not in svg
    assert uri not in svg  # the picture is paths, not the text


def test_recovery_codes_have_at_least_80_bits_and_are_matched_loosely() -> None:
    assert totp.RECOVERY_CODE_BYTES * 8 >= 80
    code = totp.new_recovery_code()
    assert len(code) == 23
    normal = totp.normalise_recovery_code(code)
    assert normal is not None
    assert totp.normalise_recovery_code(code.lower().replace("-", " ")) == normal
    assert totp.hash_recovery_code(normal) != normal
    for bad in ("", "short", "0" * 20, "A" * 21, "A" * 19 + "1"):
        assert totp.normalise_recovery_code(bad) is None


# ---- Storage and checking ----


async def test_the_secret_is_stored_encrypted_and_bound_to_the_user(app: object) -> None:
    first = await create_user("one@example.test", PASSWORD)
    second = await create_user("two@example.test", PASSWORD)
    secret = await add_authenticator(first)
    async with session() as db:
        stored = await db.get(UserTotp, first)
    assert stored is not None
    assert secret not in stored.secret
    vault = get_vault()
    assert vault.decrypt(stored.secret, totp_aad(first)) == secret
    with pytest.raises(VaultError):
        vault.decrypt(stored.secret, totp_aad(second))  # copied to another user: refused
    assert {"user_totp.secret", "auth_flows.totp_secret"} <= {f.name for f in encrypted_fields()}


async def test_key_rotation_moves_the_authenticator_secret(app: object) -> None:
    user = await create_user("one@example.test", PASSWORD)
    secret = await add_authenticator(user)
    new = generate_key("next")
    vault = Vault(
        parse_encryption_keys(f"{new},{get_settings().encryption_keys.get_secret_value()}")
    )
    results = await admin_keys.reencrypt(vault)
    assert results["user_totp.secret"].reencrypted == 1
    assert (await admin_keys.key_status(vault)).rotation_complete
    async with session() as db:
        stored = await db.get(UserTotp, user)
    assert stored is not None
    assert vault.decrypt(stored.secret, totp_aad(user)) == secret


async def test_a_code_works_once_and_older_steps_are_refused_afterwards(app: object) -> None:
    user = await create_user("one@example.test", PASSWORD)
    secret = await add_authenticator(user)
    now, earlier, later, much_later = (current_code(secret, offset) for offset in (0, -1, 1, 2))
    assert await twofactor.use_code(user, f"{now[:3]} {now[3:]}")
    assert not await twofactor.use_code(user, now)  # replay
    assert not await twofactor.use_code(user, earlier)  # older than the last accepted step
    assert await twofactor.use_code(user, later)  # a later step is fine
    assert not await twofactor.use_code(user, later)
    assert not await twofactor.use_code(user, much_later)  # outside the window


async def test_the_same_code_sent_many_times_at_once_is_accepted_once(app: object) -> None:
    user = await create_user("one@example.test", PASSWORD)
    secret = await add_authenticator(user)
    code = current_code(secret)  # computed once
    results = await asyncio.gather(*(twofactor.use_code(user, code) for _ in range(8)))
    assert results.count(True) == 1


async def test_recovery_codes_work_once_and_regeneration_replaces_them(app: object) -> None:
    user = await create_user("one@example.test", PASSWORD)
    await add_authenticator(user)
    async with write_transaction() as db:
        old = await twofactor.replace_recovery_codes(db, user)
    assert len(old) == 10
    assert await twofactor.remaining_recovery_codes(user) == 10
    assert await twofactor.use_code(user, old[0].lower())
    assert not await twofactor.use_code(user, old[0])
    assert await twofactor.remaining_recovery_codes(user) == 9
    async with write_transaction() as db:
        new = await twofactor.replace_recovery_codes(db, user)
    assert not set(new) & set(old)
    assert not await twofactor.use_code(user, old[1])
    assert await twofactor.use_code(user, new[0])
    async with session() as db:
        rows = (await db.execute(RecoveryCode.__table__.select())).all()
    assert not any(code.replace("-", "") in repr(row) for row in rows for code in new + old)


async def test_one_recovery_code_used_at_the_same_moment_counts_once(app: object) -> None:
    user = await create_user("one@example.test", PASSWORD)
    async with write_transaction() as db:
        codes = await twofactor.replace_recovery_codes(db, user)
    results = await asyncio.gather(*(twofactor.use_code(user, codes[0]) for _ in range(8)))
    assert results.count(True) == 1


async def test_a_recovery_code_of_another_user_is_refused(app: object) -> None:
    one = await create_user("one@example.test", PASSWORD)
    two = await create_user("two@example.test", PASSWORD)
    async with write_transaction() as db:
        codes = await twofactor.replace_recovery_codes(db, one)
    assert not await twofactor.use_code(two, codes[0])
    assert await twofactor.use_code(one, codes[0])
