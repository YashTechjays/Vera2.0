"""merge dev logout head with quality-cleanup head

Revision ID: d847b1292fdb
Revises: 7fb0099d5b71, 098fb25594c1
Create Date: 2026-07-08 10:17:01.001149

"""

from collections.abc import Sequence

revision: str = "d847b1292fdb"
down_revision: str | None = ("7fb0099d5b71", "098fb25594c1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
