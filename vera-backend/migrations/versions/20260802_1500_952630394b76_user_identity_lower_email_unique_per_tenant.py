"""user_identity: case-insensitive email unique per (tenant, provider)

Revision ID: 952630394b76
Revises: 9885160a381e
Create Date: 2026-08-02 15:00:00

Password login (`_load_password_creds`, api/v1/auth.py) matches on
`func.lower(email)` with `.first()`, so two identities differing only by case
resolve non-deterministically to whichever row comes back first.

Scoped per provider so a second identity for the same app_user (e.g. an SSO row
beside the password row) is untouched. Platform-tier identities have a NULL
`tenant_id`, which Postgres treats as distinct in a unique index, so they are
not covered — acceptable while a single seeded platform login provider exists.

Unlike migration 0020, this does NOT auto-de-duplicate existing rows: colliding
real accounts are a data decision, not a schema one, so a failure here is the
correct outcome — it surfaces the duplicate for manual resolution instead of
silently picking a winner.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "952630394b76"
down_revision: str | None = "9885160a381e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_user_identity_tenant_provider_lower_email"


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} "
        "ON user_identity (tenant_id, provider_type, lower(email)) "
        "WHERE email IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
