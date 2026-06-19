"""drop idempotency_key — idempotency no longer uses a Postgres table

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-17

Idempotency-Key handling moved off this table (amended ADR vera2-database-design
§707): concurrent retries are now collapsed by a short-lived Redis in-flight lock,
and durable de-duplication of late retries is a UNIQUE constraint on the resource's
natural key. Redis never stores a resource id, so there is no mapping table to keep.
The `IdempotencyKey` model is removed.

`idempotency_key` was materialized by 0001 (which builds DDL from `Base.metadata`)
with the generic tenant-isolation RLS policy, so an already-provisioned database
has the table even though its name never appeared in a migration. `DROP TABLE IF
EXISTS … CASCADE` drops it (and its RLS policies) where present, and is a no-op on a
fresh DB built after the model's removal (0001 no longer emits it).

Irreversible: the model is gone, so there is nothing to recreate; the table held
only transient reference ids with no downstream consumers. `downgrade` is
intentionally a no-op.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS idempotency_key CASCADE")


def downgrade() -> None:
    # No-op: idempotency moved to Redis; the table and its model no longer exist.
    pass
