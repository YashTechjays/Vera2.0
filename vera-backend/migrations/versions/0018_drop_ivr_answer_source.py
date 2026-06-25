"""Drop IVR from field_answer.source CHECK; add baseline lookup index

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-25

Form-fill sources are now only `intake`, `ai_call`, `human` (IVR is dropped as a fill
source). The dispute signal is derived from `field_answer` history — a current `ai_call`
value diverging from the most recent `intake`/`human` baseline — so a new index on
`(form_id, field_path, created_at DESC)` backs the baseline `DISTINCT ON` lookup.

Migration 0001 materializes table DDL from `Base.metadata` at runtime, so a DB built
fresh AFTER the model change already has the narrowed CHECK and the index. The CHECK is
dropped + recreated (Postgres has no `ALTER … ADD CONSTRAINT IF NOT EXISTS`); `ADD`
re-validates existing rows, which is fine as no `ivr`-source rows are expected. The index
uses `CREATE INDEX IF NOT EXISTS` — a no-op on fresh DBs (matches the model `Index`),
created on already-provisioned ones. The CHECK name matches the model's NAMING_CONVENTION
(`ck_%(table_name)s_%(constraint_name)s` over `check_in`'s default `source_valid`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE field_answer DROP CONSTRAINT IF EXISTS ck_field_answer_source_valid")
    op.execute(
        """
        ALTER TABLE field_answer ADD CONSTRAINT ck_field_answer_source_valid
            CHECK (source IN ('intake', 'ai_call', 'human'));
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_field_answer_baseline "
        "ON field_answer (form_id, field_path, created_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_field_answer_baseline")
    op.execute("ALTER TABLE field_answer DROP CONSTRAINT IF EXISTS ck_field_answer_source_valid")
    op.execute(
        """
        ALTER TABLE field_answer ADD CONSTRAINT ck_field_answer_source_valid
            CHECK (source IN ('intake', 'ivr', 'ai_call', 'human'));
        """
    )
