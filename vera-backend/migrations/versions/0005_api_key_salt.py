"""api_key.salt — per-key random salt for SHA-256 key hashing

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-17

Inbound API keys are verified, never recovered: the token is
`vk_<tenant_id>.<key_id>.<secret>`, and the row stores a per-key random `salt` plus
`key_hash = sha256(salt || secret)`. This adds the `salt` column the model gained.

Migration 0001 materializes table DDL from `Base.metadata`, so a database built
fresh AFTER the model change already has `salt`; an already-provisioned database
(at 0004) does not. `ADD COLUMN IF NOT EXISTS` covers both — a no-op on fresh DBs,
an additive ALTER on existing ones. The transient `DEFAULT '\\x'` backfills any
pre-existing rows (none exist pre-launch) so the NOT NULL holds, then is dropped so
the application must always supply a real salt.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_key ADD COLUMN IF NOT EXISTS salt bytea NOT NULL DEFAULT '\\x'")
    op.execute("ALTER TABLE api_key ALTER COLUMN salt DROP DEFAULT")


def downgrade() -> None:
    op.execute("ALTER TABLE api_key DROP COLUMN IF EXISTS salt")
