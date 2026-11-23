"""Per-connection switch for write actions (off by default).

Revision ID: b3e7a9c1d5f8
Revises: a8d3f1c6b247
Create Date: 2026-11-23 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7a9c1d5f8"
down_revision: str | None = "a8d3f1c6b247"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pms_connections",
        sa.Column("writes_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    with op.batch_alter_table("pms_connections") as batch:
        batch.drop_column("writes_enabled")
