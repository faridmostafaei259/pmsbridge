"""Never reuse a user id after the person is deleted (for example a cancelled invitation).

Revision ID: e5a7c3b91d24
Revises: b81e5f3a60c2
Create Date: 2026-11-22 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a7c3b91d24"
down_revision: str | None = "b81e5f3a60c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = "id, email, password_hash, role, is_active, setup_pending, created_at, last_login_at"


def _rebuild_users(*, autoincrement: bool) -> None:
    """Make the table again (SQLite cannot change the key type in place) and copy the rows.

    Migrations run with foreign keys off, so the tables that point at `users` keep their
    references while the table is replaced.
    """
    op.create_table(
        "users_new",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("setup_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("role IN ('admin', 'staff')", name=op.f("ck_users_role")),
        sa.CheckConstraint("is_active IN (0, 1)", name=op.f("ck_users_is_active")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sqlite_autoincrement=autoincrement,
    )
    op.execute(f"INSERT INTO users_new ({COLUMNS}) SELECT {COLUMNS} FROM users")  # noqa: S608
    op.drop_table("users")
    op.rename_table("users_new", "users")


def upgrade() -> None:
    _rebuild_users(autoincrement=True)


def downgrade() -> None:
    _rebuild_users(autoincrement=False)
