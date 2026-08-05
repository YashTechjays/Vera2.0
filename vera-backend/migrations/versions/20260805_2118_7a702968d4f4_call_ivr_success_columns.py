"""call ivr success columns

Revision ID: 7a702968d4f4
Revises: c247bd741862
Create Date: 2026-08-05 21:18:22.908636

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '7a702968d4f4'
down_revision: str | None = 'c247bd741862'
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
