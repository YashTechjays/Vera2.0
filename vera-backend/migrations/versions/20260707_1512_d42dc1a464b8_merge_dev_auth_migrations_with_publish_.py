"""merge dev auth migrations with publish-call heads

Revision ID: d42dc1a464b8
Revises: 467e0adaaea1, 62947d8a3be7, 808fa9885ef1
Create Date: 2026-07-07 15:12:34.469964

"""

from collections.abc import Sequence

revision: str = "d42dc1a464b8"
down_revision: str | None = ("467e0adaaea1", "62947d8a3be7", "808fa9885ef1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
