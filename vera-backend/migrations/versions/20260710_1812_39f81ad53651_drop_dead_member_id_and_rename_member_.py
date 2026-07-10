"""drop_dead_member_id_and_rename_member_policy_id

Revision ID: 39f81ad53651
Revises: 8115d1763daf
Create Date: 2026-07-10 18:12:32.830166

"""
from collections.abc import Sequence

from alembic import op

revision: str = '39f81ad53651'
down_revision: str | None = '8115d1763daf'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only act if the OLD shape is present (member_policy_id still exists) — a fresh
    # DB built from 0001's create_all off the already-renamed model never has it, so
    # this is a no-op there. Drop the dead old member_id first: it would collide with
    # the rename target.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'patient_form' AND column_name = 'member_policy_id'
            ) THEN
                ALTER TABLE patient_form DROP COLUMN IF EXISTS member_id;
                ALTER TABLE patient_form RENAME COLUMN member_policy_id TO member_id;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'patient_form' AND column_name = 'member_id'
            ) THEN
                ALTER TABLE patient_form RENAME COLUMN member_id TO member_policy_id;
                ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS member_id varchar(128);
            END IF;
        END $$;
        """
    )
