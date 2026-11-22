"""Encryption of secrets at rest (PMS credentials and tokens) with AES-256-GCM.

`ENCRYPTION_KEYS` lists the keys as `<id>:<base64 key>` entries. The first is the active key and
encrypts everything new; the others only decrypt older data until `admin keys reencrypt` has moved
it to the active key.

A stored value (bytes, for a `LargeBinary` column) is laid out as: one format-version byte, one
byte with the key id length, the key id, a 12-byte random nonce, then the ciphertext with its
authentication tag. Each call also takes an `aad` string, for example
`pms_connection:<id>:credentials`. The header (version, key id length, key id) and the `aad` are
authenticated but the `aad` is not stored, so a value copied to another row or used for another
purpose does not decrypt, and a changed header fails too.

Features register each encrypted column once with `register_encrypted_field`, at import time of
their `<feature>.db` module (listed in `storage.MODEL_MODULES`). Key rotation imports those modules
and uses the registry to find every value (see `pmsbridge.admin.keys`), so a column that is not
registered is not rotated.
"""

import base64
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Table
from sqlalchemy.orm import DeclarativeBase

from pmsbridge.core.settings import (
    ENCRYPTION_KEY_BYTES,
    KEY_ID_PATTERN,
    get_settings,
    parse_encryption_keys,
)

FORMAT_VERSION: Final = 1
NONCE_BYTES: Final = 12
TAG_BYTES: Final = 16
PREFIX_BYTES: Final = 2  # format version and key id length


class VaultError(RuntimeError):
    """A value could not be encrypted or decrypted. The message never holds keys or plaintext."""


def _associated_data(header: bytes, aad: str) -> bytes:
    """The header and the caller's `aad`, length-prefixed so the two cannot run into each other."""
    encoded = aad.encode()
    return header + len(encoded).to_bytes(4, "big") + encoded


class Vault:
    """Encrypts with the first key and decrypts with any configured key."""

    def __init__(self, keys: Sequence[tuple[str, bytes]]) -> None:
        self._ciphers = {key_id: AESGCM(key) for key_id, key in keys}
        self.active_key_id: str | None = keys[0][0] if keys else None

    @property
    def key_ids(self) -> list[str]:
        return list(self._ciphers)

    def encrypt(self, plaintext: bytes, aad: str) -> bytes:
        if self.active_key_id is None:
            raise VaultError("ENCRYPTION_KEYS is not set, so nothing can be encrypted")
        key_id = self.active_key_id.encode("ascii")
        nonce = secrets.token_bytes(NONCE_BYTES)
        header = bytes([FORMAT_VERSION, len(key_id)]) + key_id
        associated = _associated_data(header, aad)
        sealed = self._ciphers[self.active_key_id].encrypt(nonce, plaintext, associated)
        return header + nonce + sealed

    def decrypt(self, token: bytes, aad: str) -> bytes:
        key_id = self.key_id_of(token)
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise VaultError(
                f"the value was encrypted with key '{key_id}', which is not configured"
            )
        start = PREFIX_BYTES + len(key_id)
        nonce, sealed = token[start : start + NONCE_BYTES], token[start + NONCE_BYTES :]
        try:
            return cipher.decrypt(nonce, sealed, _associated_data(token[:start], aad))
        except InvalidTag:
            raise VaultError(
                "the value could not be decrypted (damaged or for another row)"
            ) from None

    @staticmethod
    def key_id_of(token: bytes) -> str:
        """The id of the key that encrypted `token`, without decrypting it."""
        if len(token) < PREFIX_BYTES or token[0] != FORMAT_VERSION:
            raise VaultError("not an encrypted value (unknown format)")
        length = token[1]
        if length == 0 or len(token) < PREFIX_BYTES + length + NONCE_BYTES + TAG_BYTES:
            raise VaultError("not an encrypted value (truncated)")
        key_id = token[PREFIX_BYTES : PREFIX_BYTES + length].decode("ascii", errors="replace")
        if not KEY_ID_PATTERN.fullmatch(key_id):
            raise VaultError("not an encrypted value (bad key id)")
        return key_id


def generate_key(key_id: str | None = None) -> str:
    """A new `<id>:<base64 key>` entry for `ENCRYPTION_KEYS`, with a random id if none is given."""
    key_id = key_id or secrets.token_hex(4)
    entry = f"{key_id}:{base64.b64encode(secrets.token_bytes(ENCRYPTION_KEY_BYTES)).decode()}"
    parse_encryption_keys(entry)  # rejects an invalid id
    return entry


_vault: tuple[str, Vault] | None = None


def get_vault() -> Vault:
    """The vault for the configured `ENCRYPTION_KEYS` (rebuilt if the setting changed)."""
    global _vault  # noqa: PLW0603
    configured = get_settings().encryption_keys.get_secret_value()
    if _vault is None or _vault[0] != configured:
        _vault = (configured, Vault(parse_encryption_keys(configured)))
    return _vault[1]


@dataclass(frozen=True)
class EncryptedField:
    model: type[DeclarativeBase]
    column_name: str
    aad_for_row: Callable[[DeclarativeBase], str]

    @property
    def name(self) -> str:
        return f"{cast(Table, self.model.__table__).name}.{self.column_name}"


_registry: dict[str, EncryptedField] = {}


def register_encrypted_field[Model: DeclarativeBase](
    model: type[Model], column_name: str, aad_for_row: Callable[[Model], str]
) -> None:
    """Declare that `model.column_name` holds `Vault.encrypt` output, bound by `aad_for_row(row)`.

    Registering the same column again replaces the earlier entry.
    """
    if column_name not in model.__table__.columns:
        raise ValueError(f"{model.__name__} has no column {column_name}")
    field = EncryptedField(model, column_name, cast(Callable[[DeclarativeBase], str], aad_for_row))
    _registry[field.name] = field


def unregister_encrypted_field(model: type[DeclarativeBase], column_name: str) -> None:
    _registry.pop(EncryptedField(model, column_name, str).name, None)


def encrypted_fields() -> list[EncryptedField]:
    return list(_registry.values())
