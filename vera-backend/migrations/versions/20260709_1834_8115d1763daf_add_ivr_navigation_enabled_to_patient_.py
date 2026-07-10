"""add ivr navigation enabled to patient form

Idempotent: migration `0001`'s `create_all` already gives a *fresh* DB this column
(off the live model), while an already-provisioned DB does not — see the
migrations bullet in CLAUDE.md.

Revision ID: 8115d1763daf
Revises: efa94eaaf3f9
Create Date: 2026-07-09 18:34:18.700931

"""

from collections.abc import Sequence

from alembic import op

revision: str = "8115d1763daf"
down_revision: str | None = "089b3e98f0b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS "
        "ivr_navigation_enabled boolean NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS ivr_navigation_enabled")
