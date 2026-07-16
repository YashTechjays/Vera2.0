"""drop call.revoked_user_ids (revoke-access feature removed)

Revision ID: 28b29b4ae0cb
Revises: 66b879b30576
Create Date: 2026-07-13 09:40:00.000000

The owner revoke-access endpoint (publish-call-to-others follow-up) was removed
by product decision; this drops its per-call deny list. DROP ... IF EXISTS keeps
the migration green on a fresh CI database, where 0001's create_all off the live
models (which no longer carry the column) never created it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "28b29b4ae0cb"
down_revision: str | None = "66b879b30576"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS revoked_user_ids")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS revoked_user_ids jsonb NOT NULL DEFAULT '[]'"
    )
