"""audit_chain_heads + audit_row_hash_at — definer read helpers for anchoring

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-22

Two SECURITY DEFINER read helpers (owned by vera_definer_owner, BYPASSRLS) used
by the WORM anchoring job: audit_chain_heads() returns the latest row per tenant
chain (seq, row_hash, count); audit_row_hash_at() returns the stored row_hash at
a (tenant, seq) so verify-against-anchor can compare an externally anchored head
to current DB state across tenants. EXECUTE defaults to PUBLIC (like the verify
fns), so the app role can call them without a cross-tenant RLS grant.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"

CHAIN_HEADS_FN = f"""
CREATE OR REPLACE FUNCTION audit_chain_heads()
RETURNS TABLE(tenant_id uuid, head_seq bigint, head_row_hash bytea, row_count bigint)
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT DISTINCT ON (a.tenant_id)
           a.tenant_id,
           a.seq AS head_seq,
           a.row_hash AS head_row_hash,
           count(*) OVER (PARTITION BY a.tenant_id) AS row_count
      FROM audit_log a
     ORDER BY a.tenant_id, a.seq DESC;
$$
"""

ROW_HASH_AT_FN = f"""
CREATE OR REPLACE FUNCTION audit_row_hash_at(p_tenant_id uuid, p_seq bigint)
RETURNS bytea
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT row_hash FROM audit_log
     WHERE tenant_id = p_tenant_id AND seq = p_seq;
$$
"""


def upgrade() -> None:
    op.execute(CHAIN_HEADS_FN)
    op.execute(ROW_HASH_AT_FN)
    op.execute(f"ALTER FUNCTION audit_chain_heads() OWNER TO {DEFINER_ROLE}")
    op.execute(f"ALTER FUNCTION audit_row_hash_at(uuid, bigint) OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS audit_chain_heads()")
    op.execute("DROP FUNCTION IF EXISTS audit_row_hash_at(uuid, bigint)")
