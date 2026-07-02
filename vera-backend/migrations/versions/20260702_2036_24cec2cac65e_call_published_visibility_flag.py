"""call published visibility flag

Revision ID: 24cec2cac65e
Revises: 0022
Create Date: 2026-07-02 20:36:13.737427

"""

from collections.abc import Sequence

from alembic import op

revision: str = "24cec2cac65e"
down_revision: str | None = "0022"
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
