"""auth_audit_log WORM hash chain — pgcrypto, seq, trigger, verifier, backfill

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-22

Per-(tenant_id) SHA-256 hash chain over auth_audit_log. A BEFORE INSERT trigger
computes seq/prev_hash/row_hash for BOTH write paths (tenant ORM insert + the
log_auth_event SECURITY DEFINER fn). A per-chain advisory xact lock serializes
same-chain inserts; INSIDE that lock the trigger both assigns seq (= prev.seq + 1)
and links prev_hash to the committed tail, so seq order == commit order and the
chain can never fork. (A pre-trigger IDENTITY would be assigned BEFORE the lock,
letting a late-committing low-seq row be buried under a higher-seq tail — multiple
rows then read the same predecessor and fork; observed in testing.) One IMMUTABLE
helper computes the hash for both the trigger and verify_auth_audit_chain().
Existing rows are backfilled in (created_at, id) order per chain. See spec
2026-06-22-auth-audit-hash-chain-design.

The chain is tamper-EVIDENT, not tamper-proof: a BYPASSRLS actor (the only role
that can UPDATE/DELETE these WORM rows at all) can edit a row and recompute every
later row_hash. HMAC/anchoring hardening is tracked in adr/devops-todo.md.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
# Fixed search_path on the definer fns (CVE-class shadowing); reused verbatim from
# the sibling migrations (0002/0004) rather than inlined per-function.
_SEARCH_PATH = "SET search_path = pg_catalog, public"

# Single source of truth for the hash (D7): the canonical, fixed-order payload
# encoding lives ONCE here so the trigger and the verifier can never drift. A
# fixed search_path is mandatory on the definer fns below (CVE-class shadowing).
#   row_hash = sha256( prev_hash || UTF8(canonical_payload) )
# created_at is rendered at UTC to microseconds so the encoding is engine-stable.
AUTH_AUDIT_ROW_HASH = """
CREATE OR REPLACE FUNCTION auth_audit_row_hash(
    p_prev bytea, p_seq bigint, p_id uuid, p_tenant_id uuid, p_app_user_id uuid,
    p_event_type text, p_ip inet, p_metadata jsonb, p_created_at timestamptz
) RETURNS bytea
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT digest(
        p_prev || convert_to(
            concat_ws('|',
                p_seq::text, p_id::text, coalesce(p_tenant_id::text, ''),
                coalesce(p_app_user_id::text, ''), p_event_type,
                coalesce(host(p_ip), ''), coalesce(p_metadata::text, '{}'),
                to_char(p_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
            ), 'UTF8'),
        'sha256');
$$
"""

# BEFORE INSERT chokepoint: fires on BOTH write paths. The advisory xact lock is
# keyed by the chain (tenant_id; a constant string for the platform chain) and
# auto-releases at txn end — same-chain inserts serialize, different chains don't
# block. INSIDE the lock we read the committed tail ONCE and derive everything
# from it: seq = tail.seq + 1 (so seq is assigned in commit order, never before
# the lock), prev_hash = tail.row_hash (genesis = 32 zero bytes for the first row,
# stored not NULL so verification is uniform). SECURITY DEFINER (owned by
# vera_definer_owner, BYPASSRLS) so the tail read sees past FORCE RLS for both the
# tenant and platform chains.
#
# The tail read is split on NULL on purpose: `tenant_id IS NOT DISTINCT FROM
# NEW.tenant_id` is NOT sargable for a non-NULL uuid (the planner can't use the
# index → a seq scan of the whole table on EVERY tenant insert, O(n²) over the
# chain's life). `tenant_id = NEW.tenant_id` is index-served by
# ix_auth_audit_log_tenant_seq; the NULL branch is served by the partial index
# ix_auth_audit_log_platform_seq. Both then get an ordered tail with no sort.
AUTH_AUDIT_CHAIN_FN = f"""
CREATE OR REPLACE FUNCTION auth_audit_chain() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_seq bigint;
    v_prev bytea;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(coalesce(NEW.tenant_id::text, '__platform__'), 0));
    IF NEW.tenant_id IS NULL THEN
        SELECT seq, row_hash INTO v_seq, v_prev
          FROM auth_audit_log
         WHERE tenant_id IS NULL
         ORDER BY seq DESC
         LIMIT 1;
    ELSE
        SELECT seq, row_hash INTO v_seq, v_prev
          FROM auth_audit_log
         WHERE tenant_id = NEW.tenant_id
         ORDER BY seq DESC
         LIMIT 1;
    END IF;
    NEW.seq := coalesce(v_seq, 0) + 1;
    NEW.prev_hash := coalesce(v_prev, decode(repeat('00', 32), 'hex'));
    NEW.row_hash := auth_audit_row_hash(
        NEW.prev_hash, NEW.seq, NEW.id, NEW.tenant_id, NEW.app_user_id,
        NEW.event_type, NEW.ip_address, NEW.metadata, NEW.created_at);
    RETURN NEW;
END;
$$
"""

# Verifier: walk one chain in seq order, recompute each row_hash via the same
# helper, and return the seq of the FIRST row whose stored hash diverges or whose
# prev_hash != the prior row's row_hash; NULL if the chain is intact. SECURITY
# DEFINER (BYPASSRLS) so ops/compliance can verify a chain (incl. the platform
# chain) without crossing RLS by hand.
VERIFY_FN = f"""
CREATE OR REPLACE FUNCTION verify_auth_audit_chain(p_tenant_id uuid)
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
        SELECT * FROM auth_audit_log
         WHERE tenant_id IS NOT DISTINCT FROM p_tenant_id
         ORDER BY seq ASC
    LOOP
        IF r.prev_hash IS DISTINCT FROM v_prev THEN
            RETURN r.seq;
        END IF;
        v_calc := auth_audit_row_hash(
            v_prev, r.seq, r.id, r.tenant_id, r.app_user_id,
            r.event_type, r.ip_address, r.metadata, r.created_at);
        IF r.row_hash IS DISTINCT FROM v_calc THEN
            RETURN r.seq;
        END IF;
        v_prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END;
$$
"""

# Backfill any pre-existing rows into the chain: assign a per-chain contiguous
# seq in (created_at, id) order and link the hashes the same way the trigger will
# — so the verifier (which walks seq ASC) sees an intact chain from row 1. Runs as
# the migration superuser (BYPASSRLS) → the WORM UPDATE is permitted here and ONLY
# here. Resets seq + prev at each chain (tenant) boundary; a v_started guard
# distinguishes the first iteration from the NULL-tenant chain.
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
        SELECT * FROM auth_audit_log
         ORDER BY tenant_id NULLS FIRST, created_at, id
    LOOP
        IF NOT v_started OR r.tenant_id IS DISTINCT FROM v_cur_tenant THEN
            v_prev := v_zero;
            v_seq := 0;
            v_cur_tenant := r.tenant_id;
            v_started := true;
        END IF;
        v_seq := v_seq + 1;
        v_hash := auth_audit_row_hash(
            v_prev, v_seq, r.id, r.tenant_id, r.app_user_id,
            r.event_type, r.ip_address, r.metadata, r.created_at);
        UPDATE auth_audit_log
           SET seq = v_seq, prev_hash = v_prev, row_hash = v_hash
         WHERE id = r.id;
        v_prev := v_hash;
    END LOOP;
END $$
"""


def upgrade() -> None:
    # --- pgcrypto (digest) + seq ordering column + chain index --------------
    # The trigger populates `seq` (it is not an IDENTITY — see the module
    # docstring), so add it NULLABLE, let the backfill fill existing rows, then
    # SET NOT NULL. On a fresh DB 0001's create_all already built `seq` NOT NULL
    # from the model, so ADD COLUMN IF NOT EXISTS is a no-op there.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("ALTER TABLE auth_audit_log ADD COLUMN IF NOT EXISTS seq bigint")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_auth_audit_log_tenant_seq ON auth_audit_log (tenant_id, seq)"
    )
    # Partial index for the platform (NULL-tenant) chain tail: the composite index
    # above degrades to a bitmap-scan + sort for `tenant_id IS NULL`, so the shared
    # platform chain gets its own ordered tail index. The tenant chains use the
    # composite index (equality on tenant_id) directly.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_auth_audit_log_platform_seq "
        "ON auth_audit_log (seq DESC) WHERE tenant_id IS NULL"
    )

    # --- definer needs SELECT to read the prior row past FORCE RLS -----------
    # 0002 granted the definer role INSERT only; the trigger + verifier read the
    # chain, so add SELECT (BYPASSRLS bypasses the policies, but the role still
    # needs the table-level privilege).
    op.execute(f"GRANT SELECT ON auth_audit_log TO {DEFINER_ROLE}")

    # --- hash helper, trigger fn, verifier ----------------------------------
    op.execute(AUTH_AUDIT_ROW_HASH)
    op.execute(AUTH_AUDIT_CHAIN_FN)
    op.execute(VERIFY_FN)
    op.execute(
        "CREATE TRIGGER trg_auth_audit_chain BEFORE INSERT ON auth_audit_log "
        "FOR EACH ROW EXECUTE FUNCTION auth_audit_chain()"
    )
    # The definer fns execute as their owner; ownership is what makes BYPASSRLS
    # apply. (The pure helper reads nothing → it can stay owned by the migration
    # role.)
    op.execute(f"ALTER FUNCTION auth_audit_chain() OWNER TO {DEFINER_ROLE}")
    op.execute(f"ALTER FUNCTION verify_auth_audit_chain(uuid) OWNER TO {DEFINER_ROLE}")

    # --- seed the chain over any pre-existing rows, then lock seq down -------
    op.execute(BACKFILL)
    op.execute("ALTER TABLE auth_audit_log ALTER COLUMN seq SET NOT NULL")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_auth_audit_chain ON auth_audit_log")
    op.execute("DROP FUNCTION IF EXISTS verify_auth_audit_chain(uuid)")
    op.execute("DROP FUNCTION IF EXISTS auth_audit_chain()")
    op.execute(
        "DROP FUNCTION IF EXISTS auth_audit_row_hash("
        "bytea, bigint, uuid, uuid, uuid, text, inet, jsonb, timestamptz)"
    )
    op.execute(f"REVOKE SELECT ON auth_audit_log FROM {DEFINER_ROLE}")
    op.execute("DROP INDEX IF EXISTS ix_auth_audit_log_platform_seq")
    op.execute("DROP INDEX IF EXISTS ix_auth_audit_log_tenant_seq")
    op.execute("ALTER TABLE auth_audit_log DROP COLUMN IF EXISTS seq")
    # Leave pgcrypto + the chained hashes in place: the table is WORM (no UPDATE
    # path) and a dev downgrade is a full reset anyway.
