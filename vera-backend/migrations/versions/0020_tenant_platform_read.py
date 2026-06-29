"""tenant: platform-readable SELECT policy

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-29

Lets a platform (SUPER_ADMIN) session read the tenant catalog (id / name / slug —
org metadata, not PHI) so an operator can pick a tenant to elevate into.

Additive and SELECT-only: the existing `tenant_tenant_isolation` policy is left
untouched, so a tenant session still sees only its own row (`id = app.tenant_id`).
This second permissive policy additionally matches when `app.platform = 'on'`
(set only by a verified platform session), and Postgres OR-combines permissive
policies — so a platform session reads all tenants while nothing changes for
tenant sessions. WITH CHECK is intentionally absent (SELECT-only): platform
operators never write the tenant table on this path.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "tenant_platform_read"


def upgrade() -> None:
    op.execute(
        f"CREATE POLICY {_POLICY} ON tenant FOR SELECT "
        "USING (current_setting('app.platform', true) = 'on')"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON tenant")
