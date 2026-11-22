"""Database tables for dashboard accounts: users and their sessions."""

from datetime import UTC, datetime
from typing import Final, override

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from pmsbridge.core.crypto import register_encrypted_field
from pmsbridge.core.storage import Base

ROLES: Final = ("admin", "staff")


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp stored as UTC. SQLite drops the zone, so it is put back when reading."""

    impl = DateTime
    cache_ok = True

    @override
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must carry a time zone")
        return value.astimezone(UTC).replace(tzinfo=None)

    @override
    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'staff')", name="role"),
        CheckConstraint("is_active IN (0, 1)", name="is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Always stored trimmed and lower-case (see `accounts.service.normalise_email`).
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="staff")
    is_active: Mapped[bool] = mapped_column(default=True)
    # True from `users create-admin` until the person has used the setup link; never signs in.
    setup_pending: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class Session(Base):
    """A signed-in browser. Only the SHA-256 of the cookie value is stored, never the value."""

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_secret: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), default=None)


class OneTimeToken(Base):
    """A setup or reset link. Only the SHA-256 is stored; one live link per person and purpose."""

    __tablename__ = "one_time_tokens"
    __table_args__ = (UniqueConstraint("user_id", "purpose"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(16))  # "setup" or "reset"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)


class AuthFlow(Base):
    """A short-lived server-side state for a multi-step page flow, found by a `__Host-` cookie.

    Kinds: `setup` and `reset` (after a link was opened), `second_factor` (password accepted, code
    still needed), `enroll` (an administrator without two-factor must enroll) and `enable`.
    """

    __tablename__ = "auth_flows"
    __table_args__ = (Index("ix_auth_flows_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # SHA-256 of the cookie value
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    token_id: Mapped[int | None] = mapped_column(
        ForeignKey("one_time_tokens.id", ondelete="CASCADE"), default=None
    )
    csrf_secret: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    totp_secret: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    next_path: Mapped[str] = mapped_column(String(2048), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)


class UserTotp(Base):
    """A confirmed authenticator. `secret` is vault-encrypted; `last_step` blocks code reuse."""

    __tablename__ = "user_totp"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    secret: Mapped[bytes] = mapped_column(LargeBinary)
    last_step: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class RecoveryCode(Base):
    """One unused recovery code, stored as a SHA-256 hash and deleted when used."""

    __tablename__ = "recovery_codes"
    __table_args__ = (Index("ix_recovery_codes_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


def totp_aad(user_id: int) -> str:
    return f"user_totp:{user_id}:secret"


def flow_aad(flow_id: str) -> str:
    return f"auth_flow:{flow_id}:totp_pending"


register_encrypted_field(UserTotp, "secret", lambda row: totp_aad(row.user_id))
register_encrypted_field(AuthFlow, "totp_secret", lambda row: flow_aad(row.id))
