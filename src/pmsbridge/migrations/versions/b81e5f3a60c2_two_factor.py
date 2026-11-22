"""Two-factor sign-in, setup links and sign-in flows.

Revision ID: b81e5f3a60c2
Revises: 7c4d2a91f3b8
Create Date: 2026-11-22 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b81e5f3a60c2"
down_revision: str | None = "7c4d2a91f3b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("setup_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "one_time_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_one_time_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_one_time_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_one_time_tokens_token_hash")),
        sa.UniqueConstraint("user_id", "purpose", name=op.f("uq_one_time_tokens_user_id")),
    )
    op.create_table(
        "auth_flows",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=True),
        sa.Column("csrf_secret", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("totp_secret", sa.LargeBinary(), nullable=True),
        sa.Column("next_path", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_flows_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["one_time_tokens.id"],
            name=op.f("fk_auth_flows_token_id_one_time_tokens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_flows")),
    )
    op.create_index("ix_auth_flows_user_id", "auth_flows", ["user_id"])
    op.create_table(
        "user_totp",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("last_step", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_totp_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_totp")),
    )
    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recovery_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_recovery_codes_code_hash")),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_recovery_codes_user_id", table_name="recovery_codes")
    op.drop_table("recovery_codes")
    op.drop_table("user_totp")
    op.drop_index("ix_auth_flows_user_id", table_name="auth_flows")
    op.drop_table("auth_flows")
    op.drop_table("one_time_tokens")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("setup_pending")
