"""form_schema.insurance_type CHECK + UNIQUE; one published schema_version per family

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-19

`form_schema.insurance_type` becomes a constrained catalog value (the `InsuranceType`
StrEnum, enforced by CHECK — never free text) and is made UNIQUE so there is exactly
one schema family per insurance type. A partial unique index on `schema_version`
guarantees at most one `status = 'published'` version per `schema_id`. Together these
give "the published schema for this insurance type" as a single indexed lookup.

Migration 0001 materializes table DDL from `Base.metadata` at runtime, so a DB built
fresh AFTER the model change already has all three objects; an already-provisioned DB
(at 0009) does not. The two indexes use `CREATE UNIQUE INDEX IF NOT EXISTS` (a no-op on
fresh DBs — `IF NOT EXISTS` matches the constraint-backed index 0001 created from the
model's `UniqueConstraint`/`Index`). Postgres has no `ADD CONSTRAINT IF NOT EXISTS` for
CHECKs, so the CHECK is wrapped in a DO block that swallows `duplicate_object` — also a
no-op on fresh DBs. The CHECK name matches the model's `NAMING_CONVENTION`
(`ck_%(table_name)s_%(constraint_name)s` over `check_in`'s default `insurance_type_valid`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE form_schema ADD CONSTRAINT ck_form_schema_insurance_type_valid
                CHECK (insurance_type IN ('infertility_treatment'));
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_form_schema_insurance_type "
        "ON form_schema (insurance_type)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_schema_version_published_per_schema "
        "ON schema_version (schema_id) WHERE status = 'published'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_schema_version_published_per_schema")
    # uq_form_schema_insurance_type is a UNIQUE *constraint* on a fresh DB (0001 built it
    # from the model) but a standalone *index* where this migration created it. Drop the
    # constraint form first (no-op if it's a plain index), then the index form.
    op.execute("ALTER TABLE form_schema DROP CONSTRAINT IF EXISTS uq_form_schema_insurance_type")
    op.execute("DROP INDEX IF EXISTS uq_form_schema_insurance_type")
    op.execute(
        "ALTER TABLE form_schema DROP CONSTRAINT IF EXISTS ck_form_schema_insurance_type_valid"
    )
