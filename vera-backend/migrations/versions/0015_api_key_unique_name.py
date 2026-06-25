"""api_key: at most one active key per (tenant, name)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-24

A partial unique index enforces a single ACTIVE (`revoked = false`) key per name
within a tenant, so the create endpoint can't mint duplicate-named keys. Partial so
revoking frees the name for reuse/rotation; tenant_id is part of the key so different
tenants may each have a key of the same name.

Existing data may already hold duplicate active names, which would block the index.
The upgrade first de-duplicates: for each (tenant_id, name) group of active keys it
keeps the most recently created one active and revokes the rest, then creates the
index. Migration 0001 materializes table DDL from `Base.metadata`, so a fresh DB
already has this index from the model — `CREATE UNIQUE INDEX IF NOT EXISTS` and the
no-op de-dup (empty table) make this migration safe on both fresh and existing DBs.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_api_key_tenant_name_active"


def upgrade() -> None:
    # Keep the newest active key per (tenant_id, name); revoke older active duplicates.
    op.execute(
        """
        UPDATE api_key SET revoked = true
        WHERE revoked = false
          AND id NOT IN (
            SELECT DISTINCT ON (tenant_id, name) id
            FROM api_key
            WHERE revoked = false
            ORDER BY tenant_id, name, created_at DESC
          )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} "
        "ON api_key (tenant_id, name) WHERE revoked = false"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
