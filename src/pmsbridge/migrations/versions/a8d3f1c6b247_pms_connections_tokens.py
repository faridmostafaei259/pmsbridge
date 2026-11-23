"""PMS connections (with encrypted credentials) and their cached access tokens.

Revision ID: a8d3f1c6b247
Revises: f6c1a8d3e592
Create Date: 2026-11-23 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d3f1c6b247"
down_revision: str | None = "f6c1a8d3e592"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pms_connections",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("credentials", sa.LargeBinary(), nullable=False),
        sa.Column("guesty_account_id", sa.String(length=64), nullable=True),
        sa.Column("client_id_hint", sa.String(length=8), nullable=False),
        sa.Column("client_id_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "provider IN ('guesty', 'hostaway')", name=op.f("ck_pms_connections_provider")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'needs_attention', 'token_budget_low')",
            name=op.f("ck_pms_connections_status"),
        ),
        sa.CheckConstraint(
            "length(label) BETWEEN 1 AND 60", name=op.f("ck_pms_connections_label_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pms_connections")),
        sa.UniqueConstraint("label", name=op.f("uq_pms_connections_label")),
        sa.UniqueConstraint("guesty_account_id", name=op.f("uq_pms_connections_guesty_account_id")),
        sa.UniqueConstraint("client_id_digest", name=op.f("uq_pms_connections_client_id_digest")),
    )
    op.create_table(
        "pms_tokens",
        sa.Column("connection_id", sa.String(length=32), nullable=False),
        sa.Column("access_token", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("obtained_at", sa.DateTime(), nullable=False),
        sa.Column("budget_remaining", sa.Integer(), nullable=True),
        sa.Column("budget_reset_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["pms_connections.id"],
            name=op.f("fk_pms_tokens_connection_id_pms_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id", name=op.f("pk_pms_tokens")),
    )


def downgrade() -> None:
    op.drop_table("pms_tokens")
    op.drop_table("pms_connections")
