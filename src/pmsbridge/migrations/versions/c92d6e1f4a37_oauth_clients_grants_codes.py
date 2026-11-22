"""OAuth clients, grants and authorization codes.

Revision ID: c92d6e1f4a37
Revises: e5a7c3b91d24
Create Date: 2026-11-22 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c92d6e1f4a37"
down_revision: str | None = "e5a7c3b91d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_secret_hash", sa.String(length=64), nullable=True),
        sa.Column("client_name", sa.String(length=100), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("token_endpoint_auth_method", sa.String(length=24), nullable=False),
        sa.Column("grant_types", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "token_endpoint_auth_method IN ('none', 'client_secret_post', 'client_secret_basic')",
            name=op.f("ck_oauth_clients_auth_method"),
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_oauth_clients")),
    )
    op.create_table(
        "oauth_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("all_connections", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowed_connection_ids", sa.JSON(), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_oauth_grants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.client_id"],
            name=op.f("fk_oauth_grants_client_id_oauth_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_grants")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_oauth_grants_user_id", "oauth_grants", ["user_id"])
    op.create_index("ix_oauth_grants_client_id", "oauth_grants", ["client_id"])
    op.create_table(
        "oauth_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("resource", sa.String(length=2048), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["oauth_grants.id"],
            name=op.f("fk_oauth_codes_grant_id_oauth_grants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.client_id"],
            name=op.f("fk_oauth_codes_client_id_oauth_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_oauth_codes_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("code_hash", name=op.f("pk_oauth_codes")),
    )
    op.create_index("ix_oauth_codes_grant_id", "oauth_codes", ["grant_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_codes_grant_id", table_name="oauth_codes")
    op.drop_table("oauth_codes")
    op.drop_index("ix_oauth_grants_client_id", table_name="oauth_grants")
    op.drop_index("ix_oauth_grants_user_id", table_name="oauth_grants")
    op.drop_table("oauth_grants")
    op.drop_table("oauth_clients")
