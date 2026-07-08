"""patient_form enqueued_by for call ownership

Revision ID: d294be3360cd
Revises: 9bd467c85c23
Create Date: 2026-07-08 11:34:42.104501

`enqueued_by_id` persists the user who queued a form so the dispatcher can
attribute `call.initiated_by_id` to them even when the call is created later
by a different actor (freed-slot dispatch, retry-at-callback).

IF NOT EXISTS: a fresh DB gets the column and FK from migration 0001.
Postgres has no ADD CONSTRAINT IF NOT EXISTS, so the FK is wrapped in a DO
block that swallows duplicate_object. The FK name matches the model's
NAMING_CONVENTION (fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d294be3360cd"
down_revision: str | None = "9bd467c85c23"
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
