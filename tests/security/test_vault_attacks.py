"""Attacks on stored ciphertext: bit flips, truncation, swapped headers, cross-row replay, fuzzing.

Every failure must be a `VaultError` (never another exception type, never a wrong plaintext) and
its message must not contain the plaintext or any key material.
"""

import base64
import random

import pytest

from pmsbridge.core.crypto import NONCE_BYTES, PREFIX_BYTES, Vault, VaultError, generate_key
from pmsbridge.core.settings import parse_encryption_keys

PLAINTEXT = b"guesty-client-secret-sentinel-0451"
AAD = "pms_connection:7:credentials"


def vault_of(*entries: str) -> Vault:
    return Vault(parse_encryption_keys(",".join(entries)))


@pytest.fixture
def entry_a() -> str:
    return generate_key("ka")


@pytest.fixture
def entry_b() -> str:
    return generate_key("kb")


def assert_clean(error: VaultError, *entries: str) -> None:
    text = str(error) + repr(error)
    assert PLAINTEXT.decode() not in text
    for entry in entries:
        key = entry.partition(":")[2]
        assert key not in text
        assert base64.b64decode(key).hex() not in text


def test_flipping_any_single_bit_anywhere_in_the_token_is_detected(entry_a: str) -> None:
    vault = vault_of(entry_a)
    token = vault.encrypt(PLAINTEXT, AAD)
    for index in range(len(token)):
        for bit in (0, 7):
            changed = bytearray(token)
            changed[index] ^= 1 << bit
            try:
                result = vault.decrypt(bytes(changed), AAD)
            except VaultError as error:
                assert_clean(error, entry_a)
            else:  # pragma: no cover - this would be the vulnerability
                pytest.fail(f"a change at byte {index} bit {bit} was accepted: {result!r}")


def test_every_truncation_and_extension_is_rejected(entry_a: str) -> None:
    vault = vault_of(entry_a)
    token = vault.encrypt(PLAINTEXT, AAD)
    for length in range(len(token)):
        with pytest.raises(VaultError):
            vault.decrypt(token[:length], AAD)
    for extra in (b"\x00", b"\xff" * 16, token):
        with pytest.raises(VaultError):
            vault.decrypt(token + extra, AAD)


def test_swapping_the_key_id_between_two_configured_keys_fails(entry_a: str, entry_b: str) -> None:
    vault = vault_of(entry_a, entry_b)
    token = vault.encrypt(PLAINTEXT, AAD)
    assert token[PREFIX_BYTES : PREFIX_BYTES + 2] == b"ka"
    forged = token[:PREFIX_BYTES] + b"kb" + token[PREFIX_BYTES + 2 :]
    with pytest.raises(VaultError) as raised:
        vault.decrypt(forged, AAD)
    assert_clean(raised.value, entry_a, entry_b)


def test_a_token_from_another_installation_with_the_same_key_id_fails(entry_a: str) -> None:
    other = generate_key("ka")  # same id, different key
    token = vault_of(other).encrypt(PLAINTEXT, AAD)
    with pytest.raises(VaultError) as raised:
        vault_of(entry_a).decrypt(token, AAD)
    assert_clean(raised.value, entry_a, other)


@pytest.mark.parametrize(
    "other_aad",
    [
        "pms_connection:8:credentials",  # another row
        "pms_connection:7:token",  # another column
        "pms_connection:7:credentials ",  # trailing space
        "PMS_CONNECTION:7:CREDENTIALS",  # case
        "",
        "pms_connection:7:credentials\x00",
        "pms_connection:7:credentials\u200b",  # zero-width space
    ],
)
def test_a_value_only_decrypts_for_its_exact_row_and_column(entry_a: str, other_aad: str) -> None:
    vault = vault_of(entry_a)
    token = vault.encrypt(PLAINTEXT, AAD)
    assert vault.decrypt(token, AAD) == PLAINTEXT
    with pytest.raises(VaultError):
        vault.decrypt(token, other_aad)


def test_the_header_and_the_context_cannot_be_traded_for_each_other(entry_a: str) -> None:
    """The context is length-prefixed, so moving bytes between header and context fails."""
    vault = vault_of(entry_a)
    first = vault.encrypt(PLAINTEXT, "ab")
    with pytest.raises(VaultError):
        vault.decrypt(first, "a")
    with pytest.raises(VaultError):
        vault.decrypt(first, "abb")


def test_nonces_do_not_repeat_across_many_encryptions(entry_a: str) -> None:
    vault = vault_of(entry_a)
    start = PREFIX_BYTES + len("ka")
    nonces = {vault.encrypt(b"x", AAD)[start : start + NONCE_BYTES] for _ in range(5000)}
    assert len(nonces) == 5000


def test_the_same_plaintext_never_gives_the_same_token(entry_a: str) -> None:
    vault = vault_of(entry_a)
    assert vault.encrypt(PLAINTEXT, AAD) != vault.encrypt(PLAINTEXT, AAD)


def test_the_plaintext_and_the_key_are_not_visible_in_the_token(entry_a: str) -> None:
    vault = vault_of(entry_a)
    token = vault.encrypt(PLAINTEXT, AAD)
    key = base64.b64decode(entry_a.partition(":")[2])
    assert PLAINTEXT not in token
    assert key not in token
    assert AAD.encode() not in token


def test_empty_and_large_values_round_trip(entry_a: str) -> None:
    vault = vault_of(entry_a)
    for value in (b"", b"\x00" * 1, b"a" * 1_000_000):
        assert vault.decrypt(vault.encrypt(value, AAD), AAD) == value


def test_random_junk_only_ever_raises_vault_error(entry_a: str) -> None:
    vault = vault_of(entry_a)
    generator = random.Random(20261122)  # noqa: S311  (a fixed seed for a repeatable fuzz run)
    valid = vault.encrypt(PLAINTEXT, AAD)
    for _ in range(3000):
        size = generator.randint(0, 80)
        junk = bytes(generator.getrandbits(8) for _ in range(size))
        # Junk that merely looks like a header gets the same treatment.
        for candidate in (junk, b"\x01\x02ka" + junk, valid[:6] + junk):
            with pytest.raises(VaultError):
                vault.decrypt(candidate, AAD)


@pytest.mark.parametrize("length_byte", [0, 17, 200, 255])
def test_a_wrong_key_id_length_byte_is_rejected(entry_a: str, length_byte: int) -> None:
    vault = vault_of(entry_a)
    token = bytearray(vault.encrypt(PLAINTEXT, AAD))
    token[1] = length_byte
    with pytest.raises(VaultError):
        vault.decrypt(bytes(token), AAD)


@pytest.mark.parametrize("version", [0, 2, 255])
def test_an_unknown_format_version_is_rejected(entry_a: str, version: int) -> None:
    vault = vault_of(entry_a)
    token = bytearray(vault.encrypt(PLAINTEXT, AAD))
    token[0] = version
    with pytest.raises(VaultError):
        vault.decrypt(bytes(token), AAD)


def test_a_key_id_with_non_ascii_or_control_bytes_is_rejected(entry_a: str) -> None:
    vault = vault_of(entry_a)
    token = vault.encrypt(PLAINTEXT, AAD)
    for bad in (b"k\xff", b"k\x00", b"K!", b"k\n"):
        forged = token[:PREFIX_BYTES] + bad + token[PREFIX_BYTES + 2 :]
        with pytest.raises(VaultError):
            vault.decrypt(forged, AAD)


def test_the_error_for_an_unknown_key_names_only_the_id(entry_a: str, entry_b: str) -> None:
    token = vault_of(entry_b).encrypt(PLAINTEXT, AAD)
    with pytest.raises(VaultError) as raised:
        vault_of(entry_a).decrypt(token, AAD)
    assert "kb" in str(raised.value)
    assert_clean(raised.value, entry_a, entry_b)
