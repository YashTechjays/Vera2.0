"""call revoked_user_ids deny list

Revision ID: 9750fbeb1fc8
Revises: 185eb795125b
Create Date: 2026-07-08 11:34:44.304501

Persist owner revocations so revoke-access is durable: join_token refuses
listed users even while the call is published. IF NOT EXISTS because a fresh
DB gets the column from migration 0001's Base.metadata materialization.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9750fbeb1fc8"
down_revision: str | None = "185eb795125b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS revoked_user_ids JSONB"
        " NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS revoked_user_ids")
