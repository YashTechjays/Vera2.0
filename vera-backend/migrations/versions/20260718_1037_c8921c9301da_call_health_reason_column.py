"""call health reason column

Revision ID: c8921c9301da
Revises: 8c10a6182907
Create Date: 2026-07-18 10:37:55.440982

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c8921c9301da"
down_revision: str | None = "8c10a6182907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: a fresh DB already has this via 0001's create_all off the live
    # models; only an already-provisioned DB needs the ADD (repo migration rule).
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS health_reason VARCHAR(500)")


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_reason")
