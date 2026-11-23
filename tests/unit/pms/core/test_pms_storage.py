import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pms_helpers import make_connection, put_token
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from pmsbridge.core import crypto
from pmsbridge.core.crypto import VaultError, get_vault
from pmsbridge.core.settings import get_settings
from pmsbridge.core.storage import (
    Base,
    alembic_config,
    import_model_modules,
    session,
    write_transaction,
)
from pmsbridge.pms.db import (
    PMSConnection,
    PMSToken,
    credentials_aad,
    open_credentials,
    token_aad,
)

REVISION = "a8d3f1c6b247"


def test_the_history_has_one_head_and_this_migration_follows_the_previous_one() -> None:
    script = ScriptDirectory.from_config(alembic_config(Path("unused.db")))
    assert len(script.get_heads()) == 1
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == "f6c1a8d3e592"


def test_the_migration_matches_the_models_and_can_be_undone() -> None:
    path = get_settings().database_path
    import_model_modules()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    assert [d for d in differences if "pms_" in repr(d)] == []
    config = alembic_config(path)
    command.downgrade(config, "f6c1a8d3e592")
    with sqlite3.connect(path) as connection:
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert "pms_connections" not in names and "pms_tokens" not in names
    command.upgrade(config, "head")


def test_both_secret_columns_are_registered_for_key_rotation() -> None:
    names = {field.name for field in crypto.encrypted_fields()}
    assert {"pms_connections.credentials", "pms_tokens.access_token"} <= names


async def test_credentials_are_bound_to_their_row() -> None:
    first = await make_connection("One")
    second = await make_connection("Two", client_id="0other-client-5678")
    async with session() as db:
        one, two = await db.get(PMSConnection, first), await db.get(PMSConnection, second)
    assert one is not None and two is not None
    assert open_credentials(one)["client_id"] != open_credentials(two)["client_id"]
    with pytest.raises(VaultError):  # a value copied to another row does not decrypt
        get_vault().decrypt(one.credentials, credentials_aad(second))
    with pytest.raises(VaultError):
        get_vault().decrypt(one.credentials, token_aad(first))


@pytest.mark.parametrize("label", ["", "x" * 61])
async def test_labels_must_be_1_to_60_characters(label: str) -> None:
    with pytest.raises(IntegrityError):
        await make_connection(label)


async def test_labels_are_unique() -> None:
    await make_connection("Same")
    with pytest.raises(IntegrityError):
        await make_connection("Same", client_id="0another-id-0000")


async def test_unknown_providers_and_statuses_are_refused() -> None:
    with pytest.raises(IntegrityError):
        await make_connection("A", provider="other")
    with pytest.raises(IntegrityError):
        await make_connection("B", status="broken")


async def test_deleting_a_connection_deletes_its_token() -> None:
    connection_id = await make_connection()
    await put_token(connection_id, "a.b.c", expires_in=timedelta(hours=1))
    async with write_transaction() as db:
        row = await db.get(PMSConnection, connection_id)
        assert row is not None
        await db.delete(row)
    async with session() as db:
        assert await db.get(PMSToken, connection_id) is None
        count = (await db.execute(text("SELECT count(*) FROM pms_tokens"))).scalar_one()
    assert count == 0


async def test_connection_ids_are_random_hex_and_hints_show_four_characters() -> None:
    connection_id = await make_connection(client_id="0abcd-client-wxyz")
    assert len(connection_id) == 32 and int(connection_id, 16) >= 0
    async with session() as db:
        row = await db.get(PMSConnection, connection_id)
    assert row is not None and row.client_id_hint == "wxyz"
    assert row.created_at.tzinfo is UTC and row.created_at <= datetime.now(UTC)
