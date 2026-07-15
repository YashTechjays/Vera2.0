"""call intervener lock columns

Revision ID: 66b879b30576
Revises: 107da9e93469
Create Date: 2026-07-11 10:30:00.000000

Single-intervener lock columns for live-call monitoring: `intervener_user_id`
(the one user currently intervening) and `intervener_claimed_at` (claim time).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "66b879b30576"
down_revision: str | None = "107da9e93469"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS intervener_user_id uuid")
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS intervener_claimed_at timestamptz")
    # Postgres has no ADD CONSTRAINT IF NOT EXISTS — guard the FK by hand.
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE call ADD CONSTRAINT fk_call_intervener_user_id_app_user
                FOREIGN KEY (intervener_user_id) REFERENCES app_user (id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS fk_call_intervener_user_id_app_user")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS intervener_claimed_at")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS intervener_user_id")
