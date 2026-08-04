"""backfill call completion pct

Revision ID: c247bd741862
Revises: 19f852dabb3c
Create Date: 2026-08-04 21:19:55.780921

Calls closed before `apply_terminal_call_status` started freezing completion_pct
carry the column default (0) forever; copy the form's current value once so
history isn't a wall of zeros.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c247bd741862"
down_revision: str | None = "19f852dabb3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_STATEMENTS: tuple[str, ...] = (
    # Idempotent: a row that already carries a frozen value no longer matches `= 0`.
    """
    UPDATE call SET completion_pct = pf.completion_pct
    FROM patient_form pf
    WHERE pf.id = call.form_id
      AND call.current_status IN ('completed', 'failed', 'no_answer', 'busy', 'canceled')
      AND call.completion_pct = 0
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Data backfill — nothing to reverse (the previous state was "never written").
    pass
