"""The audit log of AI assistant tool calls (no arguments, results or personal data).

Revision ID: a1c4e7b92d63
Revises: b3e7a9c1d5f8
Create Date: 2026-11-23 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4e7b92d63"
down_revision: str | None = "b3e7a9c1d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=32), nullable=True),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('ok', 'denied', 'error', 'timeout')", name=op.f("ck_audit_log_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_audit_log_time", "audit_log", ["time"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_time", table_name="audit_log")
    op.drop_table("audit_log")
