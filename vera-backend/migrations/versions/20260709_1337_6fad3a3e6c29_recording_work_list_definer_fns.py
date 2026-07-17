"""recording work-list definer fns

Revision ID: 6fad3a3e6c29
Revises: 7a471dd8ce48
Create Date: 2026-07-09 13:37:30.455878

Two SECURITY DEFINER work-list helpers (owned by vera_definer_owner, BYPASSRLS)
used by the recording verifier and retention sweeper: recording_pending_work()
returns non-PHI ids+pointers for PENDING rows; recording_retention_due() returns
ids for AVAILABLE rows whose retention window has expired. EXECUTE is locked to
the deployed app role (3f7a9c2e8b41 posture): these fns enumerate cross-tenant
recording pointers past RLS, so PUBLIC must not be able to call them.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "6fad3a3e6c29"
down_revision: str | None = "7a471dd8ce48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
# Deployment-specific app role that may EXECUTE the definer functions (prod: the
# Cloud SQL app role; tests: vera_rls_test). Unset (local/CI) → CURRENT_USER.
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"
_FN_SIGNATURES = ("recording_pending_work()", "recording_retention_due()")
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
     WHERE r.status = 'pending'
       AND r.egress_id IS NOT NULL;  -- a NULL egress_id can never be verified
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
    # SECURITY DEFINER runs the body AS vera_definer_owner. BYPASSRLS lets it skip
    # row security, but table-level SELECT is still required — grant it explicitly
    # (mirrors the audit_chain_heads grants in 0012/0015). Without this both fns
    # raise "permission denied for table recording" on every verifier/sweeper tick.
    op.execute(f"GRANT SELECT ON recording TO {DEFINER_ROLE}")
    # BYPASSRLS-owned fns that enumerate cross-tenant pointers: only the deployed
    # app role may call them (mirrors 3f7a9c2e8b41's platform-definer lockdown).
    for sig in _FN_SIGNATURES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS recording_pending_work()")
    op.execute("DROP FUNCTION IF EXISTS recording_retention_due()")
    op.execute(f"REVOKE SELECT ON recording FROM {DEFINER_ROLE}")
