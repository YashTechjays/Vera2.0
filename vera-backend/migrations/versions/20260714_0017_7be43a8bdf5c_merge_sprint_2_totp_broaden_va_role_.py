"""merge sprint-2 totp + broaden-va-role heads

Revision ID: 7be43a8bdf5c
Revises: 8fcbca449f35, 65ab40d4f511
Create Date: 2026-07-14 00:17:07.302452

Empty join revision: the sprint-2 TOTP chain (`8fcbca449f35`) and the
broaden-VA-role revision (`65ab40d4f511`) both fork from `3083477bf7a5`; this
unifies them so `alembic upgrade head` resolves to a single head.
"""

from collections.abc import Sequence

revision: str = "7be43a8bdf5c"
down_revision: str | None = ("8fcbca449f35", "65ab40d4f511")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
