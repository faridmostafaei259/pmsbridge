"""Helpers for the PMS core tests: connections and tokens written straight to the database."""

import secrets
from datetime import UTC, datetime, timedelta

from pmsbridge.core.storage import session, write_transaction
from pmsbridge.pms.db import PMSConnection, PMSToken, seal_token, sealed_credential_columns

CLIENT_ID = "0test-client-id-1234"
CLIENT_SECRET = "test-secret-value-not-real"  # noqa: S105  (invented)
ACCOUNT_ID = "acct-test-0001"


async def make_connection(
    label: str = "Main",
    *,
    client_id: str = CLIENT_ID,
    secret: str = CLIENT_SECRET,
    status: str = "active",
    account_id: str | None = None,
    provider: str = "guesty",
) -> str:
    connection_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    async with write_transaction() as db:
        db.add(
            PMSConnection(
                id=connection_id,
                provider=provider,
                label=label,
                status=status,
                **sealed_credential_columns(
                    connection_id, {"client_id": client_id, "client_secret": secret}
                ),
                guesty_account_id=account_id,
                created_at=now,
                updated_at=now,
            )
        )
    return connection_id


async def put_token(
    connection_id: str,
    token: str,
    *,
    expires_in: timedelta,
    remaining: int | None = None,
    reset_in: timedelta | None = None,
) -> None:
    now = datetime.now(UTC)
    async with write_transaction() as db:
        row = await db.get(PMSToken, connection_id)
        if row is None:
            row = PMSToken(connection_id=connection_id)
            db.add(row)
        row.access_token = seal_token(connection_id, token)
        row.expires_at = now + expires_in
        row.obtained_at = now - timedelta(hours=1)
        row.budget_remaining = remaining
        row.budget_reset_at = None if reset_in is None else now + reset_in


async def set_expiry(connection_id: str, expires_in: timedelta) -> None:
    async with write_transaction() as db:
        row = await db.get(PMSToken, connection_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) + expires_in


async def read_connection(connection_id: str) -> PMSConnection:
    async with session() as db:
        row = await db.get(PMSConnection, connection_id)
    assert row is not None
    return row


async def read_token_row(connection_id: str) -> PMSToken | None:
    async with session() as db:
        return await db.get(PMSToken, connection_id)
