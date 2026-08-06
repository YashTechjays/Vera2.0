"""call tenant created index

Revision ID: d7420cdbc90d
Revises: 7a702968d4f4
Create Date: 2026-08-06 14:27:47.729141

The analytics history report filters the tenant's calls by a created_at window;
without this index it scans the tenant's rows (PR #57 review, fast-follow).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d7420cdbc90d"
down_revision: str | None = "7a702968d4f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: a fresh DB already has it from 0001's create_all.
    op.execute("CREATE INDEX IF NOT EXISTS ix_call_tenant_created ON call (tenant_id, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_call_tenant_created")
