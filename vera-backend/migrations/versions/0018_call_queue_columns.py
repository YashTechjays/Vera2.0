"""Add call-queue columns to tenant and patient_form; extend FormStatus CHECK.

Revision ID: 0018
Revises: 0017_persona_tweak_event

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

revision: str = "0018_call_queue_columns"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tenant: queue config knobs ---
    op.add_column(
        "tenant", sa.Column("max_retries", sa.Integer(), nullable=False, server_default="5")
    )
    op.add_column(
        "tenant",
        sa.Column("queue_expiry_hours", sa.Integer(), nullable=False, server_default="48"),
    )

    # --- patient_form: enqueued_at + updated partial index ---
    op.add_column(
        "patient_form",
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Replace the partial index to sort on enqueued_at instead of scheduled_at.
    op.drop_index("ix_patient_form_queued", table_name="patient_form")
    op.create_index(
        "ix_patient_form_queued",
        "patient_form",
        ["enqueued_at"],
        postgresql_where=sa.text("status = 'in_queue'"),
    )

    # --- Extend the FormStatus CHECK constraint to include 'expired' ---
    op.drop_constraint("ck_patient_form_status_valid", "patient_form", type_="check")
    op.create_check_constraint(
        "ck_patient_form_status_valid",
        "patient_form",
        "status IN ('ready_for_processing', 'in_queue', 'in_call', 'ai_processing', "
        "'exception_review', 'completed', 'call_failed', 'expired')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_patient_form_status_valid", "patient_form", type_="check")
    op.create_check_constraint(
        "ck_patient_form_status_valid",
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
