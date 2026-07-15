"""log_audit_event — single-statement WORM insert for the PHI audit trail

Every audit emit previously cost three round trips in its own session
(set_config tenant GUC + INSERT + COMMIT). This SECURITY DEFINER function
collapses the insert to one statement, executing as vera_definer_owner
(BYPASSRLS) — the same sanctioned write path as log_auth_event (0002).
Trust is unchanged: tenant_id comes from the server-side AuditRecord (never
client input), exactly as it did via the GUC. The 0015 audit_chain() BEFORE
INSERT trigger still assigns seq/prev_hash/row_hash, and created_at stays on
the DB clock (server default now()). The id is supplied by the caller so the
UUIDv7 PK convention (db/base.py) holds.

Revision ID: 9514979e3fee
Revises: 65ab40d4f511
Create Date: 2026-07-14 09:52:19.491184

"""

from collections.abc import Sequence

from alembic import op

revision: str = "9514979e3fee"
down_revision: str | None = "65ab40d4f511"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
# Fixed search_path on the definer fn (CVE-class shadowing); reused verbatim
# from the sibling migrations (0002/0004/0012/0015).
_SEARCH_PATH = "SET search_path = pg_catalog, public"

_SIGNATURE = (
    "log_audit_event(uuid, uuid, text, uuid, text, text, text, text,"
    " text, text, text, jsonb, text, uuid)"
)

LOG_AUDIT_EVENT = f"""
CREATE OR REPLACE FUNCTION log_audit_event(
    p_id uuid,
    p_tenant_id uuid,
    p_actor_type text,
    p_actor_user_id uuid,
    p_actor_label text,
    p_event_type text,
    p_resource_type text,
    p_resource_id text,
    p_permission_key text,
    p_decision text,
    p_request_id text,
    p_detail jsonb,
    p_reason text,
    p_elevation_session_id uuid
) RETURNS void
LANGUAGE sql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    INSERT INTO audit_log
        (id, tenant_id, actor_type, actor_user_id, actor_label, event_type,
         resource_type, resource_id, permission_key, decision, request_id,
         detail, reason, elevation_session_id)
    VALUES
        (p_id, p_tenant_id, p_actor_type::actor_type, p_actor_user_id,
         p_actor_label, p_event_type, p_resource_type, p_resource_id,
         p_permission_key, p_decision, p_request_id,
         coalesce(p_detail, '{{}}'::jsonb), p_reason, p_elevation_session_id);
$$
"""


def upgrade() -> None:
    # BYPASSRLS skips the WORM policies but not table privileges — the owner
    # still needs INSERT (0015 granted it SELECT for the chain trigger/verifier).
    op.execute(f"GRANT INSERT ON audit_log TO {DEFINER_ROLE}")
    op.execute(LOG_AUDIT_EVENT)
    # The function executes as its owner; ownership is what makes BYPASSRLS apply.
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
    op.execute(f"REVOKE INSERT ON audit_log FROM {DEFINER_ROLE}")
