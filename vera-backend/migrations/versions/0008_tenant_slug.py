"""tenant.slug — human-readable URL tenant identifier + slug->id resolver

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-18

Tenant-scoped URLs switch from the opaque tenant UUID to a human-readable `slug`
(`/api/v1/tenants/{slug}/...`) so a user can supply their tenant at login. This adds
the unique `slug` column the model gained, plus the mechanism to resolve a slug to a
tenant id *before* any tenant RLS context exists.

`slug` is NOT NULL with no transient default / backfill: the product is pre-launch
with no data to preserve, so we keep the constraint real and unbypassed. On a fresh
DB, migration 0001 materializes the column from `Base.metadata` (so `ADD COLUMN IF NOT
EXISTS` is a no-op); on a non-empty DB the ADD errors by design — wipe and re-migrate.

The `tenant` table's RLS keys on `id` and is fail-closed (no `app.tenant_id` GUC =>
zero rows), so a login-time `SELECT ... WHERE slug = :slug` from an unpinned session
returns nothing. `resolve_tenant_by_slug` is a narrow SECURITY DEFINER function owned
by `vera_definer_owner` (NOLOGIN BYPASSRLS) — the same sanctioned pattern as
`active_elevation_grants` (0002). The app role gets no BYPASSRLS and the tenant RLS
policy is untouched; the function only ever returns the id for a given slug.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"

# Fixed search_path is mandatory on a SECURITY DEFINER fn — stops a caller shadowing
# `tenant` with an object on a search_path they control (CVE-class escalation).
RESOLVE_TENANT_BY_SLUG = """
CREATE OR REPLACE FUNCTION resolve_tenant_by_slug(p_slug text) RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT id FROM tenant WHERE slug = p_slug AND deleted_at IS NULL;
$$
"""


def upgrade() -> None:
    # No-op on a fresh DB (0001 create_all already built `slug NOT NULL`); errors on a
    # non-empty existing DB by design — see module docstring (wipe + re-migrate).
    op.execute("ALTER TABLE tenant ADD COLUMN IF NOT EXISTS slug varchar(63) NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_slug ON tenant (slug)")

    # slug -> id resolver. The app role reaches it via the default PUBLIC EXECUTE (same
    # as the 0002 definer fns); the definer needs explicit SELECT on `tenant`, and
    # ownership is what makes its BYPASSRLS apply when the body reads the table.
    op.execute(f"GRANT SELECT ON tenant TO {DEFINER_ROLE}")
    op.execute(RESOLVE_TENANT_BY_SLUG)
    op.execute(f"ALTER FUNCTION resolve_tenant_by_slug(text) OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_tenant_by_slug(text)")
    op.execute(f"REVOKE SELECT ON tenant FROM {DEFINER_ROLE}")
    # Dropping the column cascades to whatever backs uq_tenant_slug — a UNIQUE
    # CONSTRAINT on a fresh DB (0001 create_all from `unique=True`) or a standalone
    # INDEX on a DB where this migration created it. An explicit DROP INDEX would fail
    # on the constraint-backed case, so let the column drop clean it up.
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS slug")
