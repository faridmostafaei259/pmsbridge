"""Database tables for the OAuth authorization server: clients, grants, codes and tokens.

A *client* is an AI assistant that registered itself, or one that named itself with a web address
whose metadata document pmsbridge fetched (kind `metadata_document`, see `client_metadata.py`).
A *grant* is one person's consent for one client, with the PMS connections the person allowed. An
*authorization code* is the short-lived, single-use value the client exchanges for tokens. Codes
and tokens are stored only as SHA-256 hashes.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmsbridge.accounts.db import UtcDateTime
from pmsbridge.core.storage import Base

AUTH_METHODS: Final = ("none", "client_secret_post", "client_secret_basic")


class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    __table_args__ = (
        CheckConstraint(
            "token_endpoint_auth_method IN ('none', 'client_secret_post', 'client_secret_basic')",
            name="auth_method",
        ),
        Index("ix_oauth_clients_client_url", "client_url", unique=True),
    )

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # SHA-256 of the client secret; None for public clients (`token_endpoint_auth_method` none).
    client_secret_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    client_name: Mapped[str] = mapped_column(String(100))
    redirect_uris: Mapped[list[str]] = mapped_column(JSON)
    token_endpoint_auth_method: Mapped[str] = mapped_column(String(24))
    grant_types: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # Set when a person authorizes the client (and, later, when it uses a token).
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    # `dcr` (registered through /oauth/register) or `metadata_document` (the client id is a web
    # address). A metadata-document client keeps a derived short `client_id` as its key, so grants,
    # codes and tokens work unchanged; the address assistants use as client_id is `client_url`.
    kind: Mapped[str] = mapped_column(String(20), default="dcr", server_default="dcr")
    client_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    # The document is used until this time; the next authorization request after it fetches again.
    cache_until: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    @property
    def public_id(self) -> str:
        """The client id the assistant itself uses (its web address, or the registered id)."""
        return self.client_url or self.client_id


class OAuthGrant(Base):
    """One person's consent for one client. Revoking it ends every token issued under it."""

    __tablename__ = "oauth_grants"
    __table_args__ = (
        Index("ix_oauth_grants_user_id", "user_id"),
        Index("ix_oauth_grants_client_id", "client_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    # True: every connection the person has, now and later. Otherwise only the ids listed.
    all_connections: Mapped[bool] = mapped_column(Boolean, server_default=false())
    allowed_connection_ids: Mapped[list[str]] = mapped_column(JSON)
    scope: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    # Set whenever tokens are issued or refreshed under the grant (tokens themselves are purged).
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class OAuthCode(Base):
    """An authorization code, found by its SHA-256. It works once and ends after 5 minutes."""

    __tablename__ = "oauth_codes"
    __table_args__ = (Index("ix_oauth_codes_grant_id", "grant_id"),)

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[int] = mapped_column(ForeignKey("oauth_grants.id", ondelete="CASCADE"))
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    code_challenge: Mapped[str] = mapped_column(String(128))  # PKCE, method S256 only
    resource: Mapped[str] = mapped_column(String(2048))
    scope: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class OAuthToken(Base):
    """An access or refresh token, found by its SHA-256. A token is valid only while its grant is.

    A refresh token that was used is marked with `replaced_by` (the hash of its successor) and
    `revoked_at`; presenting it again is reuse, which revokes the whole grant.
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        CheckConstraint("kind IN ('access', 'refresh')", name="kind"),
        Index("ix_oauth_tokens_grant_id", "grant_id"),
        Index("ix_oauth_tokens_expires_at", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    grant_id: Mapped[int] = mapped_column(ForeignKey("oauth_grants.id", ondelete="CASCADE"))
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    resource: Mapped[str] = mapped_column(String(2048))
    scope: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    replaced_by: Mapped[str | None] = mapped_column(String(64), default=None)
