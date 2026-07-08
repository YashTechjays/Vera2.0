"""recording lifecycle columns and tenant retention days

Revision ID: 34124f06dc31
Revises: efa94eaaf3f9
Create Date: 2026-07-09 01:00:47.609254

"""

from collections.abc import Sequence

from alembic import op

revision: str = "34124f06dc31"
down_revision: str | None = "efa94eaaf3f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: a fresh DB's 0001 create_all already materialized these columns
    # from the live models; only an already-provisioned DB needs the ADDs.
    op.execute(
        "ALTER TABLE recording ADD COLUMN IF NOT EXISTS status VARCHAR(16) "
        "NOT NULL DEFAULT 'pending'"
    )
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS egress_id VARCHAR(128)")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS sha256 VARCHAR(64)")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS size_bytes BIGINT")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS duration_ms BIGINT")
    op.execute("ALTER TABLE recording ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE tenant ADD COLUMN IF NOT EXISTS recording_retention_days INTEGER")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE recording ADD CONSTRAINT ck_recording_status_valid
                CHECK (status IN ('pending','available','failed','discarded','deleted'));
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE recording DROP CONSTRAINT IF EXISTS ck_recording_status_valid")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS status")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS egress_id")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS sha256")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS size_bytes")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS duration_ms")
    op.execute("ALTER TABLE recording DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS recording_retention_days")
