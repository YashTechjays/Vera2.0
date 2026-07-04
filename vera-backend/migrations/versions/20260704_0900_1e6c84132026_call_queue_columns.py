"""Add call-queue columns to tenant and patient_form; extend FormStatus CHECK.

Revision ID: 1e6c84132026
Revises: 0022_tenant_platform_read

Adds:
- tenant.max_retries (int, default 5) — retry guard for the state machine.
- tenant.queue_expiry_hours (int, default 48) — dispatcher expiry window.
- patient_form.enqueued_at (timestamptz, nullable) — FIFO ordering column.

Also:
- Replaces the partial index ix_patient_form_queued so it sorts on enqueued_at
  (was scheduled_at) — the column the dispatcher uses for FIFO ordering.
- Widens the ck_patient_form_status_valid CHECK constraint to include 'expired'.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1e6c84132026"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These columns also live on the models, so migration 0001's
    # Base.metadata.create_all already builds them on a fresh DB. Use ADD COLUMN
    # IF NOT EXISTS (the repo pattern, see 0014) so this is a no-op there and a
    # real delta only on DBs migrated before the columns existed; the DEFAULT
    # backfills existing tenant rows for the NOT NULL columns.
    op.execute("ALTER TABLE tenant ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 5")
    op.execute(
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS queue_expiry_hours INTEGER NOT NULL DEFAULT 48"
    )
    op.execute(
        "ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS enqueued_at TIMESTAMP WITH TIME ZONE"
    )

    # Replace the partial index to sort on enqueued_at instead of scheduled_at
    # (a no-op rebuild on a fresh DB where 0001 already indexes enqueued_at).
    op.drop_index("ix_patient_form_queued", table_name="patient_form")
    op.create_index(
        "ix_patient_form_queued",
        "patient_form",
        ["enqueued_at"],
        postgresql_where=sa.text("status = 'in_queue'"),
    )

    # --- Extend the FormStatus CHECK constraint to include 'expired' ---
    # Pass the bare constraint_name ("status_valid"); the metadata naming
    # convention (ck_%(table_name)s_%(constraint_name)s) renders the full
    # "ck_patient_form_status_valid" — passing the full name double-prefixes it.
    op.drop_constraint("status_valid", "patient_form", type_="check")
    op.create_check_constraint(
        "status_valid",
        "patient_form",
        "status IN ('ready_for_processing', 'in_queue', 'in_call', 'ai_processing', "
        "'exception_review', 'completed', 'call_failed', 'expired')",
    )


def downgrade() -> None:
    op.drop_constraint("status_valid", "patient_form", type_="check")
    op.create_check_constraint(
        "status_valid",
        "patient_form",
        "status IN ('ready_for_processing', 'in_queue', 'in_call', 'ai_processing', "
        "'exception_review', 'completed', 'call_failed')",
    )
    op.drop_index("ix_patient_form_queued", table_name="patient_form")
    op.create_index(
        "ix_patient_form_queued",
        "patient_form",
        ["scheduled_at"],
        postgresql_where=sa.text("status = 'in_queue'"),
    )
    op.drop_column("patient_form", "enqueued_at")
    op.drop_column("tenant", "queue_expiry_hours")
    op.drop_column("tenant", "max_retries")
