"""merge prompt-audit head with publish-call head

Revision ID: 0573cf4c8ff3
Revises: bb0f3401df12, d847b1292fdb
Create Date: 2026-07-08 11:04:15.720111

"""

from collections.abc import Sequence

revision: str = "0573cf4c8ff3"
down_revision: str | None = ("bb0f3401df12", "d847b1292fdb")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
