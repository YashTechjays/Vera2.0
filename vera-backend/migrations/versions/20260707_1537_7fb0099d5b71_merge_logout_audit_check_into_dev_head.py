"""merge logout audit check into dev head

Revision ID: 7fb0099d5b71
Revises: 808fa9885ef1, 467e0adaaea1
Create Date: 2026-07-07 15:37:16.169683

"""

from collections.abc import Sequence

revision: str = "7fb0099d5b71"
down_revision: str | None = ("808fa9885ef1", "467e0adaaea1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
