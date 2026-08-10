"""flip ivr_navigation_enabled default to false

Revision ID: d8cee818167e
Revises: d7420cdbc90d
Create Date: 2026-08-10 14:52:45.249174

Operators now opt IN to IVR navigation per enqueue (the UI toggle defaults
unchecked), so new rows must default false. Existing rows keep their stored
choice. SET DEFAULT is a full replace, so this is naturally idempotent — safe
on a fresh DB whose 0001 create_all already materialized the new default.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8cee818167e"
down_revision: str | None = "d7420cdbc90d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient_form ALTER COLUMN ivr_navigation_enabled SET DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form ALTER COLUMN ivr_navigation_enabled SET DEFAULT true")
