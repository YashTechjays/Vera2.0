"""add app_user.invited_by for onboarding traceability

Revision ID: 808fa9885ef1
Revises: 25e54e43fcf3
Create Date: 2026-07-06 19:34:33.797352

Migration 0001 materializes table DDL from `Base.metadata` at runtime, so a DB built
fresh AFTER the model gained `invited_by` already has the column and its self-FK; an
already-provisioned DB does not. `ADD COLUMN IF NOT EXISTS` is therefore a no-op on a
fresh DB and the real add on an existing one. Postgres has no `ADD CONSTRAINT IF NOT
EXISTS`, so the FK is wrapped in a DO block that swallows `duplicate_object` (also a
no-op on fresh DBs). The FK name matches the model's `NAMING_CONVENTION`
(`fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "808fa9885ef1"
down_revision: str | None = "25e54e43fcf3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app_user ADD COLUMN IF NOT EXISTS invited_by UUID")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE app_user ADD CONSTRAINT fk_app_user_invited_by_app_user
                FOREIGN KEY (invited_by) REFERENCES app_user (id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app_user DROP CONSTRAINT IF EXISTS fk_app_user_invited_by_app_user")
    op.execute("ALTER TABLE app_user DROP COLUMN IF EXISTS invited_by")
