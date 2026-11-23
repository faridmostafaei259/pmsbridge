"""Database tables for PMS connections and their cached access tokens.

A connection holds the company's PMS credentials as one encrypted value (JSON, sealed with the vault
and bound to the connection id). A token row caches the current access token, also encrypted, so a
restart or an upgrade reuses it instead of asking the PMS for a new one (Guesty allows only five new
tokens per day).
"""

import hashlib
import json
from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, Integer, LargeBinary, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from pmsbridge.accounts.db import UtcDateTime
from pmsbridge.core.crypto import get_vault, register_encrypted_field
from pmsbridge.core.storage import Base

PROVIDERS: Final = ("guesty", "hostaway")
STATUSES: Final = ("active", "disabled", "needs_attention", "token_budget_low")
LABEL_MAX_LENGTH: Final = 60


class PMSConnection(Base):
    __tablename__ = "pms_connections"
    __table_args__ = (
        CheckConstraint("provider IN ('guesty', 'hostaway')", name="provider"),
        CheckConstraint(
            "status IN ('active', 'disabled', 'needs_attention', 'token_budget_low')",
            name="status",
        ),
        CheckConstraint(f"length(label) BETWEEN 1 AND {LABEL_MAX_LENGTH}", name="label_length"),
    )

    # 32 random hex characters. Never reused, never derived from the credentials.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(LABEL_MAX_LENGTH), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    # JSON object sealed by the vault (see `seal_credentials`).
    credentials: Mapped[bytes] = mapped_column(LargeBinary)
    # The Guesty account the credentials belong to. The same account cannot be added twice.
    guesty_account_id: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    # The last four characters of the client id (or account id), so people can tell keys apart.
    client_id_hint: Mapped[str] = mapped_column(String(8), default="")
    # SHA-256 (hex) of the client id: the same key cannot be added twice, which would share its
    # token budget. Checked with `client_id_in_use` before any token is requested.
    client_id_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class PMSToken(Base):
    """The cached access token of one connection (at most one row per connection)."""

    __tablename__ = "pms_tokens"

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("pms_connections.id", ondelete="CASCADE"), primary_key=True
    )
    # Sealed by the vault (see `seal_token`).
    access_token: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    obtained_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # From the token response headers: tokens still available, and when the next one frees up.
    budget_remaining: Mapped[int | None] = mapped_column(Integer, default=None)
    budget_reset_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


def credentials_aad(connection_id: str) -> str:
    return f"pms_connection:{connection_id}:credentials"


def token_aad(connection_id: str) -> str:
    return f"pms_token:{connection_id}"


def client_id_digest_for(client_id: str) -> str:
    return hashlib.sha256(client_id.encode()).hexdigest()


async def client_id_in_use(
    db: AsyncSession, client_id: str, *, except_connection_id: str | None = None
) -> bool:
    """True when another connection already uses this client id. Ask before requesting a token."""
    query = select(PMSConnection.id).where(
        PMSConnection.client_id_digest == client_id_digest_for(client_id)
    )
    if except_connection_id is not None:
        query = query.where(PMSConnection.id != except_connection_id)
    return await db.scalar(query) is not None


def seal_credentials(connection_id: str, credentials: dict[str, str]) -> bytes:
    return get_vault().encrypt(json.dumps(credentials).encode(), credentials_aad(connection_id))


def sealed_credential_columns(
    connection_id: str, credentials: dict[str, str]
) -> dict[str, str | bytes]:
    """The column values to store whenever credentials are sealed (new or replaced)."""
    client_id = credentials["client_id"]
    return {
        "credentials": seal_credentials(connection_id, credentials),
        "client_id_digest": client_id_digest_for(client_id),
        "client_id_hint": client_id[-4:],
    }


def open_credentials(connection: PMSConnection) -> dict[str, str]:
    """The decrypted credentials. Keep the result in memory only; never log or store it."""
    raw = get_vault().decrypt(connection.credentials, credentials_aad(connection.id))
    loaded: object = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("stored credentials are not an object")
    return {str(key): str(value) for key, value in loaded.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def seal_token(connection_id: str, access_token: str) -> bytes:
    return get_vault().encrypt(access_token.encode(), token_aad(connection_id))


def open_token(token: PMSToken) -> str:
    return get_vault().decrypt(token.access_token, token_aad(token.connection_id)).decode()


register_encrypted_field(PMSConnection, "credentials", lambda row: credentials_aad(row.id))
register_encrypted_field(PMSToken, "access_token", lambda row: token_aad(row.connection_id))
