"""merge heads

Revision ID: 79141d7f73d4
Revises: 3f8ecb6efb86, 76a992faa2c5
Create Date: 2026-07-17 09:45:16.024574

"""

from collections.abc import Sequence

revision: str = "79141d7f73d4"
down_revision: str | None = ("3f8ecb6efb86", "76a992faa2c5")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
