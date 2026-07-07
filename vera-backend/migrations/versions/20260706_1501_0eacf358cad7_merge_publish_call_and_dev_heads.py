"""merge publish-call and dev heads

Revision ID: 0eacf358cad7
Revises: 1d628cc57346, 5d7bc8c2f5ca
Create Date: 2026-07-06 15:01:08.999130

"""

from collections.abc import Sequence

revision: str = "0eacf358cad7"
down_revision: str | None = ("1d628cc57346", "5d7bc8c2f5ca")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
