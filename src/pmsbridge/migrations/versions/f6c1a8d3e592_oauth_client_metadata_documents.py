"""Clients identified by a metadata document address (kind, address, fetch and cache times).

Revision ID: f6c1a8d3e592
Revises: d4b8e2a7c015
Create Date: 2026-11-23 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c1a8d3e592"
down_revision: str | None = "d4b8e2a7c015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_clients",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="dcr"),
    )
    op.add_column("oauth_clients", sa.Column("client_url", sa.String(length=2048), nullable=True))
    op.add_column("oauth_clients", sa.Column("fetched_at", sa.DateTime(), nullable=True))
    op.add_column("oauth_clients", sa.Column("cache_until", sa.DateTime(), nullable=True))
    op.create_index("ix_oauth_clients_client_url", "oauth_clients", ["client_url"], unique=True)


def downgrade() -> None:
    # Migrations run with foreign keys off, so the rebuild keeps the grants, codes and tokens.
    op.drop_index("ix_oauth_clients_client_url", table_name="oauth_clients")
    with op.batch_alter_table("oauth_clients") as batch:
        batch.drop_column("cache_until")
        batch.drop_column("fetched_at")
        batch.drop_column("client_url")
        batch.drop_column("kind")
