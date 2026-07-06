"""merge heads

Revision ID: 1d628cc57346
Revises: 24cec2cac65e, 1e6c84132026
Create Date: 2026-07-06 09:37:26.661048

"""

from collections.abc import Sequence

revision: str = "1d628cc57346"
down_revision: str | None = ("24cec2cac65e", "1e6c84132026")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
