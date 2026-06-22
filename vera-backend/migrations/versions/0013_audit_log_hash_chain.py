"""audit_log WORM hash chain — seq, trigger, verifier, backfill

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-22

Per-(tenant_id) SHA-256 hash chain over the PHI-access audit_log, mirroring the
auth_audit_log chain (migration 0012). A BEFORE INSERT trigger assigns seq and
links prev_hash/row_hash inside a per-tenant advisory xact lock so the chain
cannot fork. One IMMUTABLE helper computes the hash for both the trigger and
verify_audit_chain(). audit_log.tenant_id is NOT NULL, so there is a single
write path and a single chain partition key (no platform chain).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"

AUDIT_ROW_HASH = """
CREATE OR REPLACE FUNCTION audit_row_hash(
    p_prev bytea, p_seq bigint, p_id uuid, p_tenant_id uuid,
    p_actor_type text, p_actor_user_id uuid, p_actor_label text,
    p_event_type text, p_resource_type text, p_resource_id text,
    p_permission_key text, p_decision text, p_request_id text,
    p_detail jsonb, p_reason text, p_elevation_session_id uuid,
    p_created_at timestamptz
) RETURNS bytea
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT digest(
        p_prev || convert_to(
            concat_ws('|',
                p_seq::text, p_id::text, coalesce(p_tenant_id::text, ''),
                p_actor_type, coalesce(p_actor_user_id::text, ''), p_actor_label,
                p_event_type, p_resource_type, p_resource_id,
                coalesce(p_permission_key, ''), coalesce(p_decision, ''),
                p_request_id, coalesce(p_detail::text, '{}'), p_reason,
                coalesce(p_elevation_session_id::text, ''),
                to_char(p_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
            ), 'UTF8'),
        'sha256');
$$
"""

AUDIT_CHAIN_FN = f"""
CREATE OR REPLACE FUNCTION audit_chain() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_seq bigint;
    v_prev bytea;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text, 0));
    SELECT seq, row_hash INTO v_seq, v_prev
      FROM audit_log
     WHERE tenant_id = NEW.tenant_id
     ORDER BY seq DESC
     LIMIT 1;
    NEW.seq := coalesce(v_seq, 0) + 1;
    NEW.prev_hash := coalesce(v_prev, decode(repeat('00', 32), 'hex'));
    NEW.row_hash := audit_row_hash(
        NEW.prev_hash, NEW.seq, NEW.id, NEW.tenant_id, NEW.actor_type::text,
        NEW.actor_user_id, NEW.actor_label, NEW.event_type, NEW.resource_type,
        NEW.resource_id, NEW.permission_key, NEW.decision, NEW.request_id,
        NEW.detail, NEW.reason, NEW.elevation_session_id, NEW.created_at);
    RETURN NEW;
END;
$$
"""

VERIFY_FN = f"""
CREATE OR REPLACE FUNCTION verify_audit_chain(p_tenant_id uuid)
RETURNS bigint
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    r record;
    v_prev bytea := decode(repeat('00', 32), 'hex');
    v_calc bytea;
BEGIN
    FOR r IN
        SELECT * FROM audit_log
         WHERE tenant_id = p_tenant_id
         ORDER BY seq ASC
    LOOP
        IF r.prev_hash IS DISTINCT FROM v_prev THEN
            RETURN r.seq;
        END IF;
        v_calc := audit_row_hash(
            v_prev, r.seq, r.id, r.tenant_id, r.actor_type::text,
            r.actor_user_id, r.actor_label, r.event_type, r.resource_type,
            r.resource_id, r.permission_key, r.decision, r.request_id,
            r.detail, r.reason, r.elevation_session_id, r.created_at);
        IF r.row_hash IS DISTINCT FROM v_calc THEN
            RETURN r.seq;
        END IF;
        v_prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END;
$$
"""

BACKFILL = """
DO $$
DECLARE
    r record;
    v_zero bytea := decode(repeat('00', 32), 'hex');
    v_prev bytea;
    v_hash bytea;
    v_seq bigint;
    v_cur_tenant uuid;
    v_started boolean := false;
BEGIN
    FOR r IN
        SELECT * FROM audit_log
         ORDER BY tenant_id, created_at, id
    LOOP
        IF NOT v_started OR r.tenant_id IS DISTINCT FROM v_cur_tenant THEN
            v_prev := v_zero;
            v_seq := 0;
            v_cur_tenant := r.tenant_id;
            v_started := true;
        END IF;
        v_seq := v_seq + 1;
        v_hash := audit_row_hash(
            v_prev, v_seq, r.id, r.tenant_id, r.actor_type::text,
            r.actor_user_id, r.actor_label, r.event_type, r.resource_type,
            r.resource_id, r.permission_key, r.decision, r.request_id,
            r.detail, r.reason, r.elevation_session_id, r.created_at);
        UPDATE audit_log SET seq = v_seq, prev_hash = v_prev, row_hash = v_hash
         WHERE id = r.id;
        v_prev := v_hash;
    END LOOP;
END $$
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS seq bigint")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_tenant_seq ON audit_log (tenant_id, seq)")
    op.execute(f"GRANT SELECT ON audit_log TO {DEFINER_ROLE}")
    op.execute(AUDIT_ROW_HASH)
    op.execute(AUDIT_CHAIN_FN)
    op.execute(VERIFY_FN)
    op.execute(
        "CREATE TRIGGER trg_audit_chain BEFORE INSERT ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_chain()"
    )
    op.execute(f"ALTER FUNCTION audit_chain() OWNER TO {DEFINER_ROLE}")
    op.execute(f"ALTER FUNCTION verify_audit_chain(uuid) OWNER TO {DEFINER_ROLE}")
    op.execute(BACKFILL)
    op.execute("ALTER TABLE audit_log ALTER COLUMN seq SET NOT NULL")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_chain ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS verify_audit_chain(uuid)")
    op.execute("DROP FUNCTION IF EXISTS audit_chain()")
    op.execute(
        "DROP FUNCTION IF EXISTS audit_row_hash("
        "bytea, bigint, uuid, uuid, text, uuid, text, text, text, text, text, text,"
        " text, jsonb, text, uuid, timestamptz)"
    )
    op.execute(f"REVOKE SELECT ON audit_log FROM {DEFINER_ROLE}")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_seq")
    op.execute("ALTER TABLE audit_log DROP COLUMN IF EXISTS seq")
