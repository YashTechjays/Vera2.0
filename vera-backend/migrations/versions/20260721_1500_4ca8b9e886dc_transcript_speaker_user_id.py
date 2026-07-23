"""transcript speaker user id

Revision ID: 4ca8b9e886dc
Revises: e05205e0a173
Create Date: 2026-07-21 15:00:00.000000

Adds `transcript.speaker_user_id` — which supervisor spoke/coached a given
transcript line (NULL for Vera, the rep, and every historical row; no
backfill). Foundation for coaching mode: unlike `call.intervener_user_id`
(one lock holder per call), multiple supervisors can coach the same call, so
attribution has to live per-row, not per-call.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "4ca8b9e886dc"
down_revision: str | None = "e05205e0a173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE transcript ADD COLUMN IF NOT EXISTS speaker_user_id uuid")
    # Postgres has no ADD CONSTRAINT IF NOT EXISTS — guard the FK by hand.
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE transcript ADD CONSTRAINT fk_transcript_speaker_user_id_app_user
                FOREIGN KEY (speaker_user_id) REFERENCES app_user (id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE transcript DROP CONSTRAINT IF EXISTS fk_transcript_speaker_user_id_app_user"
    )
    op.execute("ALTER TABLE transcript DROP COLUMN IF EXISTS speaker_user_id")
