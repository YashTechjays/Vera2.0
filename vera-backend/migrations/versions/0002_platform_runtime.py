"""platform runtime — SECURITY DEFINER write paths + platform-readable identity RLS

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-16

The platform runtime (ADR-0006 §C). Migration 0001 left the platform tier
provisioned but inert: `tenant_elevation` has only a SELECT policy, `auth_audit_log`
rejects NULL-tenant inserts, and a platform SUPER_ADMIN's own (`tenant_id IS NULL`)
`app_user`/`user_role` rows are invisible to every session. This migration lays the
sanctioned write/read paths without ever granting the app role BYPASSRLS:

- A privileged `vera_definer_owner` role (NOLOGIN, BYPASSRLS) owns four narrow,
  fixed-`search_path`, SECURITY DEFINER functions — the ONLY way to create/end an
  elevation grant, list active grants, and write a (possibly NULL-tenant) auth
  event. The app role stays RLS-bound and reaches these only via EXECUTE; WORM
  immutability and tenant isolation hold for every non-definer connection.
- `app_user` and `user_role` swap their strict tenant-isolation policy for the
  platform-readable variant: a tenant session still sees only `tenant_id = GUC`,
  while a platform session (`app.platform='on'`, no tenant GUC) sees the global
  NULL-tenant rows so SUPER_ADMIN RBAC can resolve. Writes stay strict.

GCIP login (ADR-0006 §D) is deferred — platform operators are seeded/tested as
minted sessions until it lands.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.db.rls import platform_readable_rls_policy_ddl, rls_policy_ddl
from vera_core.models.enums import AuthEvent, values_of

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"

# Tables whose global (NULL-tenant) rows a platform session must read.
PLATFORM_READABLE_TABLES = ("app_user", "user_role")

# A fixed search_path is mandatory on SECURITY DEFINER functions: it stops a
# caller from shadowing `tenant_elevation`/`auth_audit_log` (or now()/gen_random_uuid)
# with objects on a search_path they control (CVE-class privilege escalation).
_SEARCH_PATH = "SET search_path = pg_catalog, public"

CREATE_ELEVATION_GRANT = f"""
CREATE OR REPLACE FUNCTION create_elevation_grant(
    p_super_admin_user_id uuid,
    p_target_tenant_id uuid,
    p_reason text,
    p_expires_at timestamptz
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_id uuid := gen_random_uuid();
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'elevation reason is required' USING ERRCODE = 'check_violation';
    END IF;
    IF p_expires_at <= now() THEN
        RAISE EXCEPTION 'elevation expiry must be in the future' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO tenant_elevation
        (id, super_admin_user_id, target_tenant_id, reason, granted_at, expires_at, ended_at)
    VALUES
        (v_id, p_super_admin_user_id, p_target_tenant_id, p_reason, now(), p_expires_at, NULL);
    RETURN v_id;
END;
$$
"""

END_ELEVATION_GRANT = f"""
CREATE OR REPLACE FUNCTION end_elevation_grant(p_elevation_id uuid)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_ended integer;
BEGIN
    UPDATE tenant_elevation
       SET ended_at = now()
     WHERE id = p_elevation_id AND ended_at IS NULL;
    GET DIAGNOSTICS v_ended = ROW_COUNT;
    RETURN v_ended > 0;
END;
$$
"""

# Active = not ended and not yet expired. NULL filters mean "any" so the same
# function serves both the platform oversight read (no filter) and the per-request
# elevation check (both filters).
ACTIVE_ELEVATION_GRANTS = f"""
CREATE OR REPLACE FUNCTION active_elevation_grants(
    p_super_admin_user_id uuid DEFAULT NULL,
    p_target_tenant_id uuid DEFAULT NULL
) RETURNS SETOF tenant_elevation
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT *
      FROM tenant_elevation
     WHERE ended_at IS NULL
       AND expires_at > now()
       AND (p_super_admin_user_id IS NULL OR super_admin_user_id = p_super_admin_user_id)
       AND (p_target_tenant_id IS NULL OR target_tenant_id = p_target_tenant_id);
$$
"""

LOG_AUTH_EVENT = f"""
CREATE OR REPLACE FUNCTION log_auth_event(
    p_tenant_id uuid,
    p_app_user_id uuid,
    p_event_type text,
    p_ip inet,
    p_meta jsonb
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_id uuid := gen_random_uuid();
BEGIN
    INSERT INTO auth_audit_log
        (id, tenant_id, app_user_id, event_type, ip_address, metadata, created_at)
    VALUES
        (v_id, p_tenant_id, p_app_user_id, p_event_type, p_ip,
         coalesce(p_meta, '{{}}'::jsonb), now());
    RETURN v_id;
END;
$$
"""

_FUNCTIONS = (CREATE_ELEVATION_GRANT, END_ELEVATION_GRANT, ACTIVE_ELEVATION_GRANTS, LOG_AUTH_EVENT)

_DROP_FUNCTIONS = (
    "DROP FUNCTION IF EXISTS create_elevation_grant(uuid, uuid, text, timestamptz)",
    "DROP FUNCTION IF EXISTS end_elevation_grant(uuid)",
    "DROP FUNCTION IF EXISTS active_elevation_grants(uuid, uuid)",
    "DROP FUNCTION IF EXISTS log_auth_event(uuid, uuid, text, inet, jsonb)",
)


def upgrade() -> None:
    # --- one active elevation grant per operator ----------------------------
    # Replace the ineffective UNIQUE(super_admin_user_id, ended_at) from 0001
    # (NULLs are distinct, so two active grants slipped through) with a partial
    # unique index that race-safely allows at most one un-ended grant per admin.
    # On a fresh DB the index already exists (model create_all in 0001) and the
    # constraint does not — both statements are IF [NOT] EXISTS, so this is a no-op.
    op.execute(
        "ALTER TABLE tenant_elevation "
        "DROP CONSTRAINT IF EXISTS uq_tenant_elevation_super_admin_user_id"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_elevation_active "
        "ON tenant_elevation (super_admin_user_id) WHERE ended_at IS NULL"
    )

    # --- auth_audit_log accepts the platform authz events -------------------
    # Recreate the event_type CHECK from the current AuthEvent catalog so the new
    # AUTHZ_ALLOW/AUTHZ_DENY values are admitted. On a fresh DB 0001's create_all
    # already built it from the same enum, so drop+recreate is a no-op there.
    _event_vals = ", ".join(f"'{v}'" for v in values_of(AuthEvent))
    op.execute(
        "ALTER TABLE auth_audit_log DROP CONSTRAINT IF EXISTS ck_auth_audit_log_event_type_valid"
    )
    op.execute(
        "ALTER TABLE auth_audit_log ADD CONSTRAINT ck_auth_audit_log_event_type_valid "
        f"CHECK (event_type IN ({_event_vals}))"
    )

    # --- platform-readable identity/RBAC RLS --------------------------------
    # Swap the strict policy (0001) for the platform-readable one so a platform
    # session resolves NULL-tenant SUPER_ADMIN rows; tenant sessions are unchanged.
    for table in PLATFORM_READABLE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        for stmt in platform_readable_rls_policy_ddl(table):
            op.execute(stmt)

    # --- privileged definer-owner role --------------------------------------
    # NOLOGIN (never connected as) + BYPASSRLS (so the narrow functions can write
    # the WORM/no-insert-policy tables). The app role gets NEITHER attribute — it
    # reaches these tables only through EXECUTE on the functions below.
    op.execute(
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{DEFINER_ROLE}') THEN "
        f"CREATE ROLE {DEFINER_ROLE} NOLOGIN BYPASSRLS; "
        f"END IF; END $$"
    )
    # USAGE so the definer can resolve public.* (without it, unqualified names in
    # the function body resolve to "relation does not exist"), plus the narrow
    # table privileges each function needs.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {DEFINER_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON tenant_elevation TO {DEFINER_ROLE}")
    op.execute(f"GRANT INSERT ON auth_audit_log TO {DEFINER_ROLE}")

    # --- SECURITY DEFINER functions -----------------------------------------
    for fn in _FUNCTIONS:
        op.execute(fn)
    # The functions execute as their owner; ownership is what makes BYPASSRLS apply.
    for signature in (
        "create_elevation_grant(uuid, uuid, text, timestamptz)",
        "end_elevation_grant(uuid)",
        "active_elevation_grants(uuid, uuid)",
        "log_auth_event(uuid, uuid, text, inet, jsonb)",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    for stmt in _DROP_FUNCTIONS:
        op.execute(stmt)
    op.execute(f"REVOKE ALL ON tenant_elevation FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE ALL ON auth_audit_log FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {DEFINER_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {DEFINER_ROLE}")

    # Restore the strict tenant-isolation policy from 0001.
    for table in PLATFORM_READABLE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        for stmt in rls_policy_ddl(table):
            op.execute(stmt)

    # Restore the original (ineffective) unique constraint from 0001.
    op.execute("DROP INDEX IF EXISTS uq_tenant_elevation_active")
    op.execute(
        "ALTER TABLE tenant_elevation "
        "DROP CONSTRAINT IF EXISTS uq_tenant_elevation_super_admin_user_id"
    )
    op.execute(
        "ALTER TABLE tenant_elevation "
        "ADD CONSTRAINT uq_tenant_elevation_super_admin_user_id "
        "UNIQUE (super_admin_user_id, ended_at)"
    )
