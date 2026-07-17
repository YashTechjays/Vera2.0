"""call health columns

Revision ID: 8c10a6182907
Revises: 3f8ecb6efb86
Create Date: 2026-07-17 17:54:58.567667

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "8c10a6182907"
down_revision: str | None = "3f8ecb6efb86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: a fresh DB already has these via 0001's create_all off the live
    # models; only an already-provisioned DB needs the ADDs (repo migration rule).
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS health_score INTEGER")
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS health_flag VARCHAR(32)")
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS health_analyzed_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE call ADD CONSTRAINT ck_call_health_flag_valid CHECK (
                health_flag IN ('none', 'supervisor_requested', 'repeated_questions',
                                'hallucination', 'conversation_loop', 'long_silence',
                                'off_script', 'low_confidence', 'other')
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS ck_call_health_flag_valid")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_analyzed_at")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_flag")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_score")
