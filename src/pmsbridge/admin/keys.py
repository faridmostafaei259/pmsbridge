"""Encryption-key rotation, the in-container half of the host command `pmsbridge rotate-keys`
(`libexec/pmsbridge/rotate-keys`).

Run as `python -m pmsbridge.admin keys status` and `python -m pmsbridge.admin keys reencrypt`.
Neither prints secrets: only the names of the encrypted columns and counts per key id.

The host command performs this flow:

1. Generate a new key entry (`pmsbridge.core.crypto.generate_key()`) and put it FIRST in
   `ENCRYPTION_KEYS` in `.env`, keeping the old entries after it. Tell the operator to back up
   the new `.env`.
2. Restart the app so it uses the new key.
3. Run `keys reencrypt` (safe to repeat; it only changes values that are not under the first key).
4. Run `keys status`. It exits 0 only when every value uses the first key.
5. Only then remove the old entries from `ENCRYPTION_KEYS` and restart again. Tell the operator
   to keep each retired entry as long as they keep backups made before the rotation: restoring such
   a backup needs the entry back in `ENCRYPTION_KEYS`, after the active key.

Every column that holds encrypted values is found through the registry in `core.crypto`. Both
actions first import the model modules so that the registry is complete. If no column is
registered, `status` says so and exits non-zero, because rotation cannot be confirmed.

Exit codes: 0 success, 1 the work is not finished (values left on old keys, unreadable values, or
nothing registered), 2 a configuration or key error (for example `ENCRYPTION_KEYS` is not set).

`reencrypt` exits 1 if any value could not be re-encrypted (for example its key was already
removed from `ENCRYPTION_KEYS`). Such a value is left unchanged and keeps showing in `status`.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import select

from pmsbridge.core import storage
from pmsbridge.core.crypto import EncryptedField, Vault, VaultError, encrypted_fields, get_vault
from pmsbridge.core.settings import SettingsError
from pmsbridge.core.storage import session, write_transaction

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 200
MAX_BATCH_SIZE = 1000


@dataclass
class FieldStatus:
    name: str
    by_key: dict[str, int] = field(default_factory=dict[str, int])
    malformed: int = 0


@dataclass
class KeyStatus:
    active_key_id: str | None
    configured: list[str]
    fields: list[FieldStatus]

    @property
    def not_under_active(self) -> int:
        """Values that are not under the active key (including unreadable ones)."""
        under_other_keys = sum(
            count
            for item in self.fields
            for key_id, count in item.by_key.items()
            if key_id != self.active_key_id
        )
        return under_other_keys + self.malformed_total

    @property
    def malformed_total(self) -> int:
        return sum(item.malformed for item in self.fields)

    @property
    def unconfigured_in_use(self) -> list[str]:
        used = {key_id for item in self.fields for key_id in item.by_key}
        return sorted(used - set(self.configured))

    @property
    def rotation_complete(self) -> bool:
        return bool(self.fields) and self.not_under_active == 0


@dataclass
class ReencryptResult:
    reencrypted: int = 0
    already_current: int = 0
    failed: int = 0


async def key_status(vault: Vault | None = None) -> KeyStatus:
    """Count the stored values of every registered encrypted column per key id."""
    storage.import_model_modules(storage.MODEL_MODULES)
    vault = vault or get_vault()
    fields: list[FieldStatus] = []
    for item in encrypted_fields():
        status = FieldStatus(item.name)
        column = getattr(item.model, item.column_name)
        async with session() as reader:
            values = (await reader.execute(select(column).where(column.is_not(None)))).scalars()
            for value in values:
                try:
                    key_id = vault.key_id_of(value)
                except VaultError:
                    status.malformed += 1
                else:
                    status.by_key[key_id] = status.by_key.get(key_id, 0) + 1
        fields.append(status)
    return KeyStatus(vault.active_key_id, vault.key_ids, fields)


async def _reencrypt_field(
    item: EncryptedField, vault: Vault, batch_size: int, result: ReencryptResult
) -> None:
    """Re-encrypt one column in batches, one transaction per batch.

    A model with a single-column primary key is paged by key (`WHERE pk > last`). A model with a
    composite key falls back to offset paging, which is correct here because rows are only updated,
    never added or removed, by this loop.
    """
    primary_key = list(item.model.__mapper__.primary_key)
    key_column = primary_key[0] if len(primary_key) == 1 else None
    last: object = None
    offset = 0
    while True:
        query = select(item.model).order_by(*primary_key).limit(batch_size)
        if key_column is None:
            query = query.offset(offset)
        elif last is not None:
            query = query.where(key_column > last)
        async with write_transaction() as writer:
            rows = (await writer.execute(query)).scalars().all()
            for row in rows:
                value: bytes | None = getattr(row, item.column_name)
                if value is None:
                    continue
                try:
                    if vault.key_id_of(value) == vault.active_key_id:
                        result.already_current += 1
                        continue
                    aad = item.aad_for_row(row)
                    setattr(row, item.column_name, vault.encrypt(vault.decrypt(value, aad), aad))
                except VaultError:
                    result.failed += 1
                else:
                    result.reencrypted += 1
            if rows and key_column is not None:
                key_name = item.model.__mapper__.get_property_by_column(key_column).key
                last = getattr(rows[-1], key_name)
        if len(rows) < batch_size:  # a short or empty batch is the last one
            return
        offset += batch_size


async def reencrypt(
    vault: Vault | None = None, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict[str, ReencryptResult]:
    """Re-encrypt every value that is not under the active key. Safe to run again."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    storage.import_model_modules(storage.MODEL_MODULES)
    vault = vault or get_vault()
    if vault.active_key_id is None:
        raise VaultError("ENCRYPTION_KEYS is not set, so nothing can be encrypted")
    results: dict[str, ReencryptResult] = {}
    for item in encrypted_fields():
        results[item.name] = ReencryptResult()
        await _reencrypt_field(item, vault, batch_size, results[item.name])
    return results


def _report_status(status: KeyStatus) -> int:
    out = sys.stdout
    out.write(f"Active key: {status.active_key_id}\n")
    out.write(f"Configured keys: {', '.join(status.configured) or 'none'}\n")
    for item in status.fields:
        counts = ", ".join(f"{key_id}={count}" for key_id, count in sorted(item.by_key.items()))
        unreadable = f", unreadable={item.malformed}" if item.malformed else ""
        out.write(f"{item.name}: {counts or 'empty'}{unreadable}\n")
    for key_id in status.unconfigured_in_use:
        out.write(f"WARNING: values are encrypted with key '{key_id}', which is not configured.\n")
    if not status.fields:
        out.write("No encrypted columns are registered, so rotation cannot be confirmed.\n")
        return 1
    if status.rotation_complete:
        out.write("Every value uses the active key. Old keys can be removed.\n")
        return 0
    out.write("Some values are not under the active key. Run: keys reencrypt\n")
    return 1


def _fail(error: Exception) -> int:
    sys.stderr.write(f"Error: {error}\nNext step: check ENCRYPTION_KEYS in .env, then try again.\n")
    return 2


def _run_status(_: argparse.Namespace) -> int:
    try:
        vault = get_vault()
        if vault.active_key_id is None:
            raise VaultError("ENCRYPTION_KEYS is not set")
        return _report_status(asyncio.run(key_status(vault)))
    except (VaultError, SettingsError) as error:
        return _fail(error)


def _run_reencrypt(args: argparse.Namespace) -> int:
    try:
        results = asyncio.run(reencrypt(batch_size=args.batch_size))
    except (VaultError, SettingsError) as error:
        return _fail(error)
    failed = 0
    for name, result in results.items():
        sys.stdout.write(
            f"{name}: re-encrypted={result.reencrypted}, already current={result.already_current}, "
            f"failed={result.failed}\n"
        )
        failed += result.failed
    logger.info(
        "admin keys reencrypt",
        extra={
            "reencrypted": sum(r.reencrypted for r in results.values()),
            "failed": failed,
        },
    )
    if failed:
        sys.stdout.write("Some values could not be re-encrypted; they were left unchanged.\n")
        return 1
    return 0


def _batch_size(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        number = 0
    if not 1 <= number <= MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_BATCH_SIZE}")
    return number


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",  # pyright: ignore[reportPrivateUsage]
) -> None:
    keys = subparsers.add_parser("keys", help="Encryption-key status and rotation.")
    actions = keys.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="Show which key each stored secret uses.").set_defaults(
        handler=_run_status
    )
    run = actions.add_parser("reencrypt", help="Re-encrypt every secret under the active key.")
    run.add_argument("--batch-size", type=_batch_size, default=DEFAULT_BATCH_SIZE)
    run.set_defaults(handler=_run_reencrypt)
