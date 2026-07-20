"""call health columns

Revision ID: 8c10a6182907
Revises: 3f8ecb6efb86
Create Date: 2026-07-17 17:54:58.567667

"""

from collections.abc import Sequence

from alembic import op

revision: str = "8c10a6182907"
down_revision: str | None = "3f8ecb6efb86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Statements run by `upgrade()`, exposed for the migration integration test
#: (mirrors `test_promoted_fields_cleanup_migration.py`'s `DELETE_STATEMENTS`
#: convention) so the test executes the exact DDL this migration runs.
UPGRADE_STATEMENTS: tuple[str, ...] = (
    # Idempotent: a fresh DB already has these via 0001's create_all off the live
    # models; only an already-provisioned DB needs the ADDs (repo migration rule).
    "ALTER TABLE call ADD COLUMN IF NOT EXISTS health_score INTEGER",
    "ALTER TABLE call ADD COLUMN IF NOT EXISTS health_flag VARCHAR(32)",
    "ALTER TABLE call ADD COLUMN IF NOT EXISTS health_analyzed_at TIMESTAMP WITH TIME ZONE",
    # NOT VALID: skips scanning existing rows, so this takes a brief SHARE UPDATE
    # EXCLUSIVE lock instead of an ACCESS EXCLUSIVE one that would block reads/writes
    # for the scan's duration on a table with real volume.
    """
    DO $$ BEGIN
        ALTER TABLE call ADD CONSTRAINT ck_call_health_flag_valid CHECK (
            health_flag IN ('none', 'supervisor_requested', 'repeated_questions',
                            'hallucination', 'conversation_loop', 'long_silence',
                            'off_script', 'low_confidence', 'other')
        ) NOT VALID;
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    # Separate VALIDATE step: still only SHARE UPDATE EXCLUSIVE (concurrent reads/
    # writes proceed), and a no-op if the constraint is already validated.
    "ALTER TABLE call VALIDATE CONSTRAINT ck_call_health_flag_valid",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS ck_call_health_flag_valid")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_analyzed_at")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_flag")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_score")
