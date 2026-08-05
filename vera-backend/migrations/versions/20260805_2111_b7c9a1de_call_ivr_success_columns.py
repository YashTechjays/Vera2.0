"""call ivr success columns

Revision ID: b7c9a1de
Revises: af1d4c138eba
Create Date: 2026-08-05 21:11:00.000000

"""

from collections.abc import Sequence

from alembic import op


revision: str = "b7c9a1de"
down_revision: str | None = "af1d4c138eba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_STATEMENTS: tuple[str, ...] = (
    # Idempotent: a fresh DB already has these from 0001's create_all.
    "ALTER TABLE call ADD COLUMN IF NOT EXISTS ivr_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE call ADD COLUMN IF NOT EXISTS ivr_exited_at TIMESTAMPTZ",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS ivr_exited_at")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS ivr_enabled")
