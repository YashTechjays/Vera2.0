"""widen audit_log.resource_id 128 -> 512 for nested route paths

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-17

`audit_log.resource_id` holds the endpoint path for authz audit rows. Nested
tenant routes carry several UUIDs (e.g.
`/api/v1/tenants/{id}/users/{id}/roles/{id}` is ~138 chars), overflowing the
original `varchar(128)`. Widen to 512.

Idempotent: altering to the same type on a fresh DB (0001 builds it at 512 from
current metadata) is a no-op-shaped rewrite; on an existing DB it widens in place.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_log ALTER COLUMN resource_id TYPE varchar(512)")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE audit_log ALTER COLUMN resource_id TYPE varchar(128)"
        " USING left(resource_id, 128)"
    )
