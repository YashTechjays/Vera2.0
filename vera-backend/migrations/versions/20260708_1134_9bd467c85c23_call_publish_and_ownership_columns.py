"""call publish and ownership columns

Revision ID: 9bd467c85c23
Revises: 7fb0099d5b71
Create Date: 2026-07-08 11:34:41.196474

IF NOT EXISTS throughout: a fresh DB gets these from migration 0001's
Base.metadata materialization; only an existing DB needs the real delta.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9bd467c85c23"
down_revision: str | None = "7fb0099d5b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS published boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS published_at timestamptz NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_call_tenant_published ON call (tenant_id, published)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_call_tenant_published")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS published_at")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS published")
