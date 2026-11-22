"""Baseline: an empty schema that later migrations build on.

Revision ID: a3f1c9d27b40
Revises:
Create Date: 2026-11-22 00:00:00
"""

from collections.abc import Sequence

revision: str = "a3f1c9d27b40"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to create yet: features add their tables in their own migrations."""


def downgrade() -> None:
    """Nothing to remove."""
