"""merge platform-mfa and insurance-providers heads

Revision ID: 2cee51fc1076
Revises: 3f7a9c2e8b41, d97ece8ca1b2
Create Date: 2026-07-10 03:31:11.885866

"""

from collections.abc import Sequence

revision: str = "2cee51fc1076"
down_revision: str | None = ("3f7a9c2e8b41", "d97ece8ca1b2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
