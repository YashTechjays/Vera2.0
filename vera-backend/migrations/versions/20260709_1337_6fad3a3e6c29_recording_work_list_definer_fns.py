"""recording work-list definer fns

Revision ID: 6fad3a3e6c29
Revises: 7a471dd8ce48
Create Date: 2026-07-09 13:37:30.455878

Two SECURITY DEFINER work-list helpers (owned by vera_definer_owner, BYPASSRLS)
used by the recording verifier and retention sweeper: recording_pending_work()
returns non-PHI ids+pointers for PENDING rows; recording_retention_due() returns
ids for AVAILABLE rows whose retention window has expired. EXECUTE defaults to
PUBLIC so the app role can call them without a cross-tenant RLS grant, following
the audit_chain_heads() precedent (migration 0016).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6fad3a3e6c29"
down_revision: str | None = "7a471dd8ce48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"

_PENDING_FN = f"""
CREATE OR REPLACE FUNCTION recording_pending_work()
RETURNS TABLE(tenant_id uuid, recording_id uuid, call_id uuid, egress_id text, gcs_uri text)
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT r.tenant_id, r.id, r.call_id, r.egress_id, r.gcs_uri
      FROM recording r
     WHERE r.status = 'pending';
$$
"""

_RETENTION_FN = f"""
CREATE OR REPLACE FUNCTION recording_retention_due()
RETURNS TABLE(tenant_id uuid, recording_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT r.tenant_id, r.id
      FROM recording r
     WHERE r.status = 'available'
       AND r.retention_until IS NOT NULL
       AND r.retention_until < now();
$$
"""


def upgrade() -> None:
    op.execute(_PENDING_FN)
    op.execute(_RETENTION_FN)
    op.execute(f"ALTER FUNCTION recording_pending_work() OWNER TO {DEFINER_ROLE}")
    op.execute(f"ALTER FUNCTION recording_retention_due() OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS recording_pending_work()")
    op.execute("DROP FUNCTION IF EXISTS recording_retention_due()")
