"""merge password-reset and tenant-retry-config heads

Revision ID: 9885160a381e
Revises: 38fbe1f6de7c, a047f496c95b
Create Date: 2026-08-01 14:23:36.015533

"""

from collections.abc import Sequence

revision: str = "9885160a381e"
down_revision: str | None = ("38fbe1f6de7c", "a047f496c95b")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
