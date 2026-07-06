"""merge fix/schema-dsl and dev migration heads

Revision ID: b3b3ab9b5099
Revises: 1e6c84132026, 8085a84daf96
Create Date: 2026-07-06 10:53:23.073699

"""

from collections.abc import Sequence

revision: str = "b3b3ab9b5099"
down_revision: str | None = ("1e6c84132026", "8085a84daf96")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
