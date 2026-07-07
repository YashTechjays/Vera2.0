"""disease_only insurance type check constraint

Revision ID: 8085a84daf96
Revises: 211edd19b786
Create Date: 2026-07-05 12:33:22.044434

`InsuranceType` grows a `disease_only` member; `form_schema.insurance_type` is
CHECK-constrained to the enum catalog (see `check_in` in vera_core.models.enums),
so the constraint is dropped and recreated with the new value list. Postgres has
no `ALTER CONSTRAINT` for CHECK bodies — drop + re-add is the supported path.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8085a84daf96"
down_revision: str | None = "211edd19b786"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_form_schema_insurance_type_valid"


def upgrade() -> None:
    op.execute(f"ALTER TABLE form_schema DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE form_schema ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (insurance_type IN ('infertility_treatment', 'disease_only'))"
    )


def downgrade() -> None:
    # Rows carrying the removed value would violate the narrowed CHECK; delete the
    # disease_only schema family first (schema_version rows cascade via FK).
    op.execute("DELETE FROM form_schema WHERE insurance_type = 'disease_only'")
    op.execute(f"ALTER TABLE form_schema DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE form_schema ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (insurance_type IN ('infertility_treatment'))"
    )
