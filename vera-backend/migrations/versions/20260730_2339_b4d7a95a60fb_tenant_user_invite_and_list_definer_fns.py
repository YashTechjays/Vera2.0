"""tenant user invite and list definer fns

Revision ID: b4d7a95a60fb
Revises: e529f5cac06d
Create Date: 2026-07-30 23:39:00.310142

The write path a platform operator uses to invite a user INTO a chosen tenant
(VR2-30), and the read path that lists that tenant's users — neither previously
possible from the platform plane at all: `app_user`/`user_role` RLS
(`... OR (tenant_id IS NULL AND app.platform = 'on')`) lets a platform session see
only NULL-tenant (platform-operator) rows, never a tenant's rows, in either
direction. Same shape as the tenant CRUD functions (e529f5cac06d): narrow, fixed
search_path, `app.platform` GUC guard, EXECUTE revoked from PUBLIC.

No elevation grant is required (spec decision, VR2-30 plan): inviting a user is
administration, not a PHI read, so it does not go through the break-glass
`tenant_elevation` flow the way viewing a tenant's *patient* data would.

Deliberately narrow: `platform_invite_tenant_user` only inserts the `app_user` row
and its `user_role` grants — it does not mint the invite token (Redis, not SQL) or
send the email (both stay in the router, exactly like the existing tenant-admin
`invite_user`). `platform_list_tenant_users` is read-only (STABLE) and returns only
the fields the Tenants-users screen shows. Role assignment through this path is
restricted (at the router, before either function is called) to global
(`tenant_id IS NULL`) non-platform-tier system roles — those are the only roles a
platform session can validate via ordinary RLS-gated reads (the `role_tenant_isolation`
/ `role_permission_tenant_isolation` policies both carry an `OR tenant_id IS NULL`
clause); a tenant's own custom roles are out of scope for this endpoint.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "b4d7a95a60fb"
down_revision: str | None = "e529f5cac06d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"

_INVITE_SIG = "platform_invite_tenant_user(uuid, uuid, text, text, uuid, uuid[], uuid[])"
_LIST_SIG = "platform_list_tenant_users(uuid)"

# Returns 'no_tenant' | 'duplicate' | 'ok' so the router can tell a 404 from a 409
# from success in one round trip, the same shape as platform_set_tenant_status.
# p_grant_ids[i] is the id for the user_role row granting p_role_ids[i]: user_role.id
# has no server default either (same UUIDv7 gap as tenant.id/app_user.id — ADR-0002),
# so the caller mints one per grant instead of the function generating it.
_INVITE_TENANT_USER = """
CREATE OR REPLACE FUNCTION platform_invite_tenant_user(
    p_tenant_id uuid,
    p_user_id uuid,
    p_email text,
    p_name text,
    p_invited_by uuid,
    p_role_ids uuid[],
    p_grant_ids uuid[]
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_i integer;
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_invite_tenant_user: not a platform session';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM tenant WHERE id = p_tenant_id AND deleted_at IS NULL) THEN
        RETURN 'no_tenant';
    END IF;
    -- No UNIQUE constraint on email (matches invite_user's own comment) — this
    -- durable de-dup check is the only guard against a double-invite.
    IF EXISTS (SELECT 1 FROM app_user WHERE tenant_id = p_tenant_id AND email = p_email) THEN
        RETURN 'duplicate';
    END IF;

    INSERT INTO app_user (id, tenant_id, email, name, status, account_type, invited_by)
    VALUES (p_user_id, p_tenant_id, p_email, p_name, 'invited', 'tenant', p_invited_by);

    IF p_role_ids IS NOT NULL THEN
        FOR v_i IN 1 .. array_length(p_role_ids, 1) LOOP
            INSERT INTO user_role (id, tenant_id, app_user_id, role_id, granted_by, granted_at)
            VALUES (
                p_grant_ids[v_i], p_tenant_id, p_user_id, p_role_ids[v_i], p_invited_by, now()
            );
        END LOOP;
    END IF;

    RETURN 'ok';
END;
$$
"""

# Explicit ::text casts: app_user's columns are varchar(n), but RETURNS TABLE declared
# text — Postgres requires an exact type match, not just assignment-compatibility.
_LIST_TENANT_USERS = """
CREATE OR REPLACE FUNCTION platform_list_tenant_users(p_tenant_id uuid)
RETURNS TABLE(id uuid, email text, name text, status text, roles text[])
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_list_tenant_users: not a platform session';
    END IF;

    RETURN QUERY
    SELECT
        u.id,
        u.email::text,
        u.name::text,
        u.status::text,
        COALESCE(
            ARRAY_AGG(r.name::text ORDER BY r.name) FILTER (WHERE r.name IS NOT NULL),
            ARRAY[]::text[]
        ) AS roles
    FROM app_user u
    LEFT JOIN user_role ur ON ur.app_user_id = u.id
    LEFT JOIN role r ON r.id = ur.role_id
    WHERE u.tenant_id = p_tenant_id
    GROUP BY u.id, u.email, u.name, u.status
    ORDER BY u.email;
END;
$$
"""

_APP_USER_INSERT_COLUMNS = (
    "id",
    "tenant_id",
    "email",
    "name",
    "status",
    "account_type",
    "invited_by",
)
_APP_USER_SELECT_COLUMNS = ("id", "tenant_id", "email", "name", "status")
_USER_ROLE_INSERT_COLUMNS = (
    "id",
    "tenant_id",
    "app_user_id",
    "role_id",
    "granted_by",
    "granted_at",
)
_USER_ROLE_SELECT_COLUMNS = ("app_user_id", "role_id")

_FUNCTIONS = (
    (_INVITE_TENANT_USER, _INVITE_SIG),
    (_LIST_TENANT_USERS, _LIST_SIG),
)


def upgrade() -> None:
    op.execute(
        f"GRANT SELECT ({', '.join(_APP_USER_SELECT_COLUMNS)}) ON app_user TO {DEFINER_ROLE}"
    )
    op.execute(
        f"GRANT INSERT ({', '.join(_APP_USER_INSERT_COLUMNS)}) ON app_user TO {DEFINER_ROLE}"
    )
    op.execute(
        f"GRANT SELECT ({', '.join(_USER_ROLE_SELECT_COLUMNS)}) ON user_role TO {DEFINER_ROLE}"
    )
    op.execute(
        f"GRANT INSERT ({', '.join(_USER_ROLE_INSERT_COLUMNS)}) ON user_role TO {DEFINER_ROLE}"
    )
    op.execute(f"GRANT SELECT (id, name) ON role TO {DEFINER_ROLE}")

    for body, signature in _FUNCTIONS:
        op.execute(body)
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {DEFINER_ROLE}")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")


def downgrade() -> None:
    for _, signature in _FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM {_APP_ROLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    op.execute(f"REVOKE SELECT (id, name) ON role FROM {DEFINER_ROLE}")
    op.execute(
        f"REVOKE INSERT ({', '.join(_USER_ROLE_INSERT_COLUMNS)}) ON user_role FROM {DEFINER_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT ({', '.join(_USER_ROLE_SELECT_COLUMNS)}) ON user_role FROM {DEFINER_ROLE}"
    )
    op.execute(
        f"REVOKE INSERT ({', '.join(_APP_USER_INSERT_COLUMNS)}) ON app_user FROM {DEFINER_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT ({', '.join(_APP_USER_SELECT_COLUMNS)}) ON app_user FROM {DEFINER_ROLE}"
    )
