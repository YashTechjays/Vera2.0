"""merge heads

Revision ID: c731b0194422
Revises: 79141d7f73d4, e05205e0a173
Create Date: 2026-07-22 01:29:45.001696

"""

from collections.abc import Sequence

revision: str = "c731b0194422"
down_revision: str | None = ("79141d7f73d4", "e05205e0a173")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
