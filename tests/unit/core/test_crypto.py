import argparse
import asyncio
import base64
import json
import logging
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import LargeBinary, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pmsbridge.admin import keys as admin_keys
from pmsbridge.core import crypto, storage
from pmsbridge.core.crypto import (
    Vault,
    VaultError,
    generate_key,
    get_vault,
    register_encrypted_field,
    unregister_encrypted_field,
)
from pmsbridge.core.logging import configure_logging
from pmsbridge.core.settings import Settings, override_settings, parse_encryption_keys
from pmsbridge.core.storage import Database, session, write_transaction


def make_vault(*entries: str) -> Vault:
    return Vault(parse_encryption_keys(",".join(entries)))


# ---- Vault ----


def test_round_trip_and_token_names_the_key() -> None:
    vault = make_vault(generate_key("k1"))
    token = vault.encrypt(b"secret value", "row:1:credentials")
    assert b"secret value" not in token
    assert vault.key_id_of(token) == "k1"
    assert vault.decrypt(token, "row:1:credentials") == b"secret value"


def test_every_encryption_uses_a_new_nonce() -> None:
    vault = make_vault(generate_key("k1"))
    assert vault.encrypt(b"x", "a") != vault.encrypt(b"x", "a")


def test_wrong_aad_fails() -> None:
    vault = make_vault(generate_key("k1"))
    token = vault.encrypt(b"x", "row:1:credentials")
    with pytest.raises(VaultError):
        vault.decrypt(token, "row:2:credentials")


def test_tampered_ciphertext_fails() -> None:
    vault = make_vault(generate_key("k1"))
    token = bytearray(vault.encrypt(b"x", "a"))
    token[-1] ^= 1
    with pytest.raises(VaultError, match="could not be decrypted"):
        vault.decrypt(bytes(token), "a")


@pytest.mark.parametrize("junk", [b"", b"\x01", b"\x02\x02k1" + bytes(40), b"\x01\x00" + bytes(40)])
def test_malformed_tokens_fail_clearly(junk: bytes) -> None:
    with pytest.raises(VaultError, match="not an encrypted value"):
        make_vault(generate_key("k1")).decrypt(junk, "a")


def test_a_body_one_byte_short_of_nonce_and_tag_is_truncated() -> None:
    header = b"\x01\x02k1"
    with pytest.raises(VaultError, match="truncated"):
        Vault.key_id_of(header + bytes(27))
    with pytest.raises(VaultError, match="could not be decrypted"):
        make_vault(generate_key("k1")).decrypt(header + bytes(28), "a")


@pytest.mark.parametrize(
    "key_id", [b"K1", b"a b", b"\x00\x01", b"k\n", b"\xff\xfe", b"a" * 17, b"k-1"]
)
def test_a_key_id_that_breaks_the_rule_is_rejected(key_id: bytes) -> None:
    token = bytes([1, len(key_id)]) + key_id + bytes(28)
    with pytest.raises(VaultError, match="bad key id"):
        Vault.key_id_of(token)


def test_a_flipped_nonce_byte_fails() -> None:
    vault = make_vault(generate_key("k1"))
    token = bytearray(vault.encrypt(b"x", "a"))
    token[2 + 2] ^= 1  # the first nonce byte follows the two-byte prefix and the id
    with pytest.raises(VaultError, match="could not be decrypted"):
        vault.decrypt(bytes(token), "a")


def test_the_header_is_authenticated() -> None:
    entry = generate_key("a")
    same_key = entry.split(":")[1]
    vault = make_vault(entry, f"b:{same_key}")
    token = vault.encrypt(b"x", "row")
    assert vault.key_id_of(token) == "a"
    changed = token[:2] + b"b" + token[3:]
    assert vault.key_id_of(changed) == "b"
    with pytest.raises(VaultError, match="could not be decrypted"):
        vault.decrypt(changed, "row")


def test_an_old_key_still_decrypts_but_new_data_uses_the_first_key() -> None:
    old, new = generate_key("old"), generate_key("new")
    token = make_vault(old).encrypt(b"x", "a")
    vault = make_vault(new, old)
    assert vault.decrypt(token, "a") == b"x"
    assert vault.key_id_of(vault.encrypt(b"x", "a")) == "new"


def test_an_unknown_key_id_fails_clearly_without_key_material() -> None:
    token = make_vault(generate_key("gone")).encrypt(b"x", "a")
    with pytest.raises(VaultError, match="key 'gone', which is not configured"):
        make_vault(generate_key("k1")).decrypt(token, "a")


def test_without_keys_the_vault_refuses_to_encrypt() -> None:
    with pytest.raises(VaultError, match="ENCRYPTION_KEYS is not set"):
        Vault([]).encrypt(b"x", "a")


def test_get_vault_follows_the_settings() -> None:
    entry = generate_key("k1")
    with override_settings(Settings(environment="development", encryption_keys=entry)):  # type: ignore[arg-type]
        assert get_vault().active_key_id == "k1"
    with override_settings(Settings(environment="development")):
        assert get_vault().active_key_id is None


def test_generate_key_gives_a_valid_entry_and_rejects_a_bad_id() -> None:
    entry = generate_key()
    key_id, encoded = entry.split(":")
    assert len(base64.b64decode(encoded)) == 32
    assert parse_encryption_keys(entry)[0][0] == key_id
    with pytest.raises(ValueError, match="entry 1"):
        generate_key("Bad Id")


# ---- Registry, status and re-encryption ----


class TestBase(DeclarativeBase):
    __test__ = False


class Secret(TestBase):
    __tablename__ = "test_secret"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class Pair(TestBase):
    __tablename__ = "test_pair"
    left: Mapped[int] = mapped_column(primary_key=True)
    right: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[bytes] = mapped_column(LargeBinary)


def aad(row: Secret) -> str:
    return f"test_secret:{row.id}:value"


@pytest.fixture
def registered() -> Iterator[None]:
    register_encrypted_field(Secret, "value", aad)
    yield
    unregister_encrypted_field(Secret, "value")


@pytest.fixture
async def secrets_table(
    database: Database, registered: None, dev_settings: Settings
) -> AsyncIterator[None]:
    with override_settings(dev_settings):
        async with database.engine.begin() as connection:
            await connection.run_sync(TestBase.metadata.create_all)
        yield


async def store(vault: Vault, count: int) -> None:
    async with write_transaction() as writer:
        for number in range(1, count + 1):
            row = Secret(id=number)
            row.value = vault.encrypt(f"plain-{number}".encode(), aad(row))
            writer.add(row)
        writer.add(Secret(id=count + 1, value=None))


async def load(vault: Vault) -> dict[int, bytes | None]:
    async with session() as reader:
        rows = (await reader.execute(select(Secret))).scalars().all()
    return {
        row.id: None if row.value is None else vault.decrypt(row.value, aad(row)) for row in rows
    }


@pytest.mark.parametrize("size", [0, -1, 1001])
async def test_reencrypt_rejects_a_batch_size_outside_the_bounds(size: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await admin_keys.reencrypt(make_vault(generate_key("k1")), batch_size=size)


@pytest.mark.parametrize("size", ["0", "-5", "1001", "many"])
def test_the_command_line_rejects_a_bad_batch_size(
    size: str, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = argparse.ArgumentParser()
    admin_keys.register(parser.add_subparsers(dest="feature", required=True))
    with pytest.raises(SystemExit):
        parser.parse_args(["keys", "reencrypt", "--batch-size", size])
    assert "from 1 to 1000" in capsys.readouterr().err


def test_registering_an_unknown_column_is_rejected() -> None:
    with pytest.raises(ValueError, match="no column"):
        register_encrypted_field(Secret, "missing", aad)


async def test_status_and_reencrypt_move_everything_to_the_active_key(
    secrets_table: None,
) -> None:
    old, new = generate_key("old"), generate_key("new")
    await store(make_vault(old), 5)
    vault = make_vault(new, old)

    before = await admin_keys.key_status(vault)
    assert before.fields[0].by_key == {"old": 5}
    assert not before.rotation_complete
    assert before.unconfigured_in_use == []

    results = await admin_keys.reencrypt(vault, batch_size=2)
    assert (results["test_secret.value"].reencrypted, results["test_secret.value"].failed) == (5, 0)

    after = await admin_keys.key_status(vault)
    assert after.fields[0].by_key == {"new": 5}
    assert after.rotation_complete

    again = await admin_keys.reencrypt(vault, batch_size=2)
    assert again["test_secret.value"].reencrypted == 0
    assert again["test_secret.value"].already_current == 5

    values = await load(vault)
    assert values[3] == b"plain-3"
    assert values[6] is None


async def test_status_reports_a_key_that_is_no_longer_configured(secrets_table: None) -> None:
    await store(make_vault(generate_key("gone")), 2)
    vault = make_vault(generate_key("new"))
    status = await admin_keys.key_status(vault)
    assert status.unconfigured_in_use == ["gone"]
    results = await admin_keys.reencrypt(vault)
    assert results["test_secret.value"].failed == 2
    assert (await admin_keys.key_status(vault)).unconfigured_in_use == ["gone"]


async def test_a_value_moved_to_another_row_fails_and_is_left_alone(secrets_table: None) -> None:
    old_entry = generate_key("old")
    async with write_transaction() as writer:
        wrong_row = make_vault(old_entry).encrypt(b"x", "test_secret:99:value")
        writer.add(Secret(id=1, value=wrong_row))
    results = await admin_keys.reencrypt(make_vault(generate_key("new"), old_entry))
    assert results["test_secret.value"].failed == 1
    async with session() as reader:
        row = (await reader.execute(select(Secret))).scalars().one()
    assert row.value is not None
    assert Vault.key_id_of(row.value) == "old"


async def test_reencrypt_needs_an_active_key(secrets_table: None) -> None:
    with pytest.raises(VaultError):
        await admin_keys.reencrypt(Vault([]))


# ---- Logs and the command line ----


async def test_the_command_line_runs_and_logs_without_secrets(
    secrets_table: None,
    dev_settings: Settings,
    restore_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old, new = generate_key("old"), generate_key("new")
    await store(make_vault(old), 2)
    settings = Settings(
        environment="development",
        data_dir=dev_settings.data_dir,
        encryption_keys=f"{new},{old}",  # type: ignore[arg-type]
    )
    parser = argparse.ArgumentParser()
    admin_keys.register(parser.add_subparsers(dest="feature", required=True))
    with override_settings(settings):
        configure_logging(settings)
        logging.getLogger("pmsbridge.test").warning(
            "settings: %r", settings, extra={"encryption_keys": settings.encryption_keys}
        )
        status_args = parser.parse_args(["keys", "status"])
        reencrypt_args = parser.parse_args(["keys", "reencrypt", "--batch-size", "1"])
        # The handlers start their own event loop, so run them off this one.
        assert await asyncio.to_thread(status_args.handler, status_args) == 1
        assert await asyncio.to_thread(reencrypt_args.handler, reencrypt_args) == 0
        assert await asyncio.to_thread(status_args.handler, status_args) == 0
    out = capsys.readouterr().out
    assert "test_secret.value: new=2" in out
    for entry in (old, new):
        assert entry.split(":")[1] not in out
    for line in out.splitlines():
        if line.startswith("{"):
            assert json.loads(line)["logger"]
    assert "plain-" not in out


# ---- The command line without the manual registration ----

MODEL_SOURCE = """
from sqlalchemy import LargeBinary
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pmsbridge.core.crypto import register_encrypted_field


class Base(DeclarativeBase):
    pass


class Secret(Base):
    __tablename__ = "test_secret"
    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


register_encrypted_field(Secret, "value", lambda row: f"test_secret:{row.id}:value")
"""


def run_cli(*arguments: str) -> int:
    parser = argparse.ArgumentParser()
    admin_keys.register(parser.add_subparsers(dest="feature", required=True))
    args = parser.parse_args(["keys", *arguments])
    code: int = args.handler(args)
    return code


async def test_the_command_line_finds_columns_registered_by_the_model_modules(
    database: Database,
    dev_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = "pmsbridge_test_models"
    (tmp_path / f"{module}.py").write_text(MODEL_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(storage, "MODEL_MODULES", (module,))
    old, new = generate_key("old"), generate_key("new")
    old_vault = make_vault(old)
    async with database.engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE test_secret (id INTEGER PRIMARY KEY, value BLOB)")
        )
        for number in (1, 2, 3):
            token = old_vault.encrypt(b"x", f"test_secret:{number}:value")
            await connection.execute(
                text("INSERT INTO test_secret VALUES (:id, :value)"), {"id": number, "value": token}
            )
    settings = Settings(
        environment="development",
        data_dir=dev_settings.data_dir,
        encryption_keys=f"{new},{old}",  # type: ignore[arg-type]
    )
    try:
        with override_settings(settings):
            assert await asyncio.to_thread(run_cli, "status") == 1
            assert "test_secret.value: old=3" in capsys.readouterr().out
            assert await asyncio.to_thread(run_cli, "reencrypt", "--batch-size", "2") == 0
            assert await asyncio.to_thread(run_cli, "status") == 0
            assert "Old keys can be removed" in capsys.readouterr().out
    finally:
        sys.modules.pop(module, None)
        crypto._registry.pop("test_secret.value", None)


async def test_status_with_nothing_registered_does_not_allow_removing_old_keys(
    database: Database,
    dev_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "MODEL_MODULES", ())
    settings = Settings(
        environment="development",
        data_dir=dev_settings.data_dir,
        encryption_keys=generate_key("k1"),  # type: ignore[arg-type]
    )
    with override_settings(settings):
        assert await asyncio.to_thread(run_cli, "status") == 1
    out = capsys.readouterr().out
    assert "No encrypted columns are registered" in out
    assert "removed" not in out


async def test_a_missing_or_bad_key_setting_gives_one_plain_line_and_exit_2(
    database: Database,
    dev_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(storage, "MODEL_MODULES", ())
    with override_settings(dev_settings):  # development, no keys
        assert await asyncio.to_thread(run_cli, "status") == 2
        assert await asyncio.to_thread(run_cli, "reencrypt") == 2
    err = capsys.readouterr().err
    assert err.count("Error: ENCRYPTION_KEYS is not set") == 2
    assert "Next step:" in err
    assert "Traceback" not in err
    monkeypatch.setenv("ENCRYPTION_KEYS", "not-valid")
    assert await asyncio.to_thread(run_cli, "status") == 2
    assert "Error: Invalid configuration" in capsys.readouterr().err


async def test_a_model_with_a_composite_key_is_re_encrypted_with_offset_paging(
    database: Database, dev_settings: Settings
) -> None:
    def pair_aad(row: Pair) -> str:
        return f"test_pair:{row.left}:{row.right}"

    register_encrypted_field(Pair, "value", pair_aad)
    old, new = generate_key("old"), generate_key("new")
    try:
        with override_settings(dev_settings):
            async with database.engine.begin() as connection:
                await connection.run_sync(TestBase.metadata.create_all)
            async with write_transaction() as writer:
                for left, right in [(1, 1), (1, 2), (2, 1)]:
                    token = make_vault(old).encrypt(b"x", f"test_pair:{left}:{right}")
                    writer.add(Pair(left=left, right=right, value=token))
            vault = make_vault(new, old)
            results = await admin_keys.reencrypt(vault, batch_size=2)
            assert results["test_pair.value"].reencrypted == 3
            status = await admin_keys.key_status(vault)
            assert {f.name: f.by_key for f in status.fields}["test_pair.value"] == {"new": 3}
    finally:
        unregister_encrypted_field(Pair, "value")
