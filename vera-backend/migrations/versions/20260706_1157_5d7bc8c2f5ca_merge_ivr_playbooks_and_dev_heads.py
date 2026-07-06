"""merge ivr-playbooks and dev heads

Revision ID: 5d7bc8c2f5ca
Revises: 0e78b863d8a3, b3b3ab9b5099
Create Date: 2026-07-06 11:57:35.589312

"""

from collections.abc import Sequence

revision: str = "5d7bc8c2f5ca"
down_revision: str | None = ("0e78b863d8a3", "b3b3ab9b5099")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
