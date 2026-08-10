"""flip ivr_navigation_enabled default to false

Revision ID: d8cee818167e
Revises: d7420cdbc90d
Create Date: 2026-08-10 14:52:45.249174

Operators now opt IN to IVR navigation per enqueue, so new rows default false;
existing rows keep their stored choice.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8cee818167e"
down_revision: str | None = "d7420cdbc90d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: SET DEFAULT replaces whatever 0001's create_all left in place.
    op.execute("ALTER TABLE patient_form ALTER COLUMN ivr_navigation_enabled SET DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form ALTER COLUMN ivr_navigation_enabled SET DEFAULT true")
