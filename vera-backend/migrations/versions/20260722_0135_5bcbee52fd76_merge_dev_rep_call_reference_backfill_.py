"""merge dev (rep_call_reference backfill) and review-and-export heads

Revision ID: 5bcbee52fd76
Revises: 79141d7f73d4, e05205e0a173
Create Date: 2026-07-22 01:35:57.911634

"""

from collections.abc import Sequence

revision: str = "5bcbee52fd76"
down_revision: str | None = ("79141d7f73d4", "e05205e0a173")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
