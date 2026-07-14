"""user_identity totp_last_used_timestep

Revision ID: bec7cbf6fed0
Revises: 3083477bf7a5
Create Date: 2026-07-13 17:29:04.762647

"""

from collections.abc import Sequence

from alembic import op

revision: str = "bec7cbf6fed0"
down_revision: str | None = "3083477bf7a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS totp_last_used_timestep BIGINT")


def downgrade() -> None:
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS totp_last_used_timestep")
