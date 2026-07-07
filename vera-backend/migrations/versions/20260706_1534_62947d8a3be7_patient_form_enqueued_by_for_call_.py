"""patient_form enqueued_by for call ownership

Revision ID: 62947d8a3be7
Revises: 0eacf358cad7
Create Date: 2026-07-06 15:34:23.375912

`enqueued_by_id` persists the user who queued a form so the dispatcher can
attribute `call.initiated_by_id` to them even when the call is actually
created later by a different actor (freed-slot dispatch, retry-at-callback).

Migration 0001 materializes table DDL from `Base.metadata` at runtime, so a DB
built fresh AFTER the model gained `enqueued_by_id` already has the column and
its FK; an already-provisioned DB does not. `ADD COLUMN IF NOT EXISTS` is
therefore a no-op on a fresh DB and the real add on an existing one. Postgres
has no `ADD CONSTRAINT IF NOT EXISTS`, so the FK is wrapped in a DO block that
swallows `duplicate_object` (also a no-op on fresh DBs). The FK name matches
the model's `NAMING_CONVENTION`
(`fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`).

"""

from collections.abc import Sequence

from alembic import op

revision: str = "62947d8a3be7"
down_revision: str | None = "0eacf358cad7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS enqueued_by_id UUID")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE patient_form ADD CONSTRAINT fk_patient_form_enqueued_by_id_app_user
                FOREIGN KEY (enqueued_by_id) REFERENCES app_user (id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE patient_form DROP CONSTRAINT IF EXISTS fk_patient_form_enqueued_by_id_app_user"
    )
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS enqueued_by_id")
