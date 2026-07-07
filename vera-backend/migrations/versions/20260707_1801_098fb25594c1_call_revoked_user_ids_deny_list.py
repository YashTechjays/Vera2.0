"""call revoked_user_ids deny list

Revision ID: 098fb25594c1
Revises: 3770eb6d5aad
Create Date: 2026-07-07 18:01:58.207230

Persist owner revocations so revoke-access is durable: join_token refuses
listed users even while the call is published. IF NOT EXISTS because a fresh
DB gets the column from migration 0001's Base.metadata materialization.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "098fb25594c1"
down_revision: str | None = "3770eb6d5aad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS revoked_user_ids JSONB"
        " NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS revoked_user_ids")
