"""patient_form: promote worklist display columns out of intake_payload

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-24

Lifts four worklist display fields into typed columns on `patient_form` so the
list endpoint selects columns instead of parsing `intake_payload` per row:
`appointment_type`, `member_policy_id`, `insurance_provider`,
`insurance_provider_phone_number`. They are projection-only (no search over them
yet), so no index is added; they are PHI under CMEK like the other promoted
identifiers.

Migration 0001 materializes table DDL from `Base.metadata` at runtime, so a DB
built fresh AFTER the model change already has these columns; an already-provisioned
DB (at 0013) does not. `ADD COLUMN IF NOT EXISTS` is therefore a no-op on a fresh DB
and the real add on an existing one.

The backfill mirrors `forms.intake.promote_columns` (same intake_payload paths,
trim/empty -> NULL) so rows uploaded before this migration show the same value the
read path used to dig out of the JSON. Rows whose payload lacks a field stay NULL.
On a fresh DB the table is empty, so the UPDATE is a no-op.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "appointment_type VARCHAR(64)",
    "member_policy_id VARCHAR(128)",
    "insurance_provider VARCHAR(255)",
    "insurance_provider_phone_number VARCHAR(64)",
)


# (column, intake_payload section, field) — keep in sync with promote_columns.
_BACKFILL = (
    ("appointment_type", "appointment_information", "appointment_type"),
    ("member_policy_id", "insurance_information", "policy_number"),
    ("insurance_provider", "insurance_reference_information", "insurance"),
    ("insurance_provider_phone_number", "insurance_reference_information", "phone_number"),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS {column}")
    for col, section, field in _BACKFILL:
        op.execute(
            f"UPDATE patient_form "
            f"SET {col} = NULLIF(TRIM(intake_payload->'{section}'->>'{field}'), '') "
            f"WHERE {col} IS NULL"
        )


def downgrade() -> None:
    for column in _COLUMNS:
        name = column.split(" ", 1)[0]
        op.execute(f"ALTER TABLE patient_form DROP COLUMN IF EXISTS {name}")
