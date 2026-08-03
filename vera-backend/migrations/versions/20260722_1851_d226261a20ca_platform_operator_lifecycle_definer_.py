"""platform operator lifecycle definer functions

Revision ID: d226261a20ca
Revises: fdce499c1b8d
Create Date: 2026-07-22 18:51:41.063720

The platform-operator invite/accept/deactivate lifecycle needs to INSERT/UPDATE
NULL-tenant app_user / user_identity / user_role rows. The platform-readable RLS
policy's WITH CHECK is strict equality (vera_core/db/rls.py), so the RLS-bound app
role can never write a NULL-tenant row directly — same restriction that migration
f066c667ddc1 worked around for platform MFA enrollment, now extended to the
invite/deactivate lifecycle. Mirror that sanctioned pattern: narrow, fixed-search_path
SECURITY DEFINER functions owned by vera_definer_owner (NOLOGIN, BYPASSRLS), each
guarded by current_setting('app.platform', true) = 'on' so only a platform session
can invoke them.

That GUC guard alone is not a privilege boundary — `app.platform` is an ordinary
session-settable custom GUC, so any DB principal that can run SQL could `SET
app.platform = 'on'` itself. The actual boundary is EXECUTE: Postgres grants EXECUTE
on a new function to PUBLIC by default, so (mirroring migration 3f7a9c2e8b41's
lockdown of the f066c667ddc1 functions, added after PR #68 review flagged exactly
this gap) this migration also revokes PUBLIC's default EXECUTE and grants it only to
the deployed app role, so the GUC guard is only ever reachable from a connection
already trusted to open a platform session.

Each guard reads `IS NOT TRUE`, not a bare `NOT (...)` — see `_not_platform_session_guard`'s
docstring below for why a plain `NOT (...)` would silently fail open on an unset GUC.

DELETE is unaffected by this restriction (RLS only evaluates USING, not WITH CHECK,
for DELETE) — the invite-resend flow's stale-identity cleanup stays a plain ORM
delete and needs no definer function.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "d226261a20ca"
down_revision: str | None = "fdce499c1b8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"
_GUARD = "current_setting('app.platform', true) = 'on'"

# Deployment-specific app role that may EXECUTE the definer functions (prod: the Cloud
# SQL app role; tests: vera_rls_test). Templated at deploy time; unset (local/CI, where
# migrations run as the app user) -> CURRENT_USER. Mirrors 3f7a9c2e8b41's convention.
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"


def _not_platform_session_guard(fn_name: str) -> str:
    """The `IF ({_GUARD}) IS NOT TRUE THEN RAISE` block shared by all three
    functions, parameterized only by the RAISE message's function name.

    Deliberately `IS NOT TRUE`, not a bare `NOT (...)`: current_setting(..., true)
    returns NULL — not 'off' — on a connection that never called `set_platform` (an
    ordinary tenant session), and plpgsql's `IF NOT (NULL)` evaluates to NULL, which
    is falsy, so the RAISE would silently never fire and the function would proceed
    as if authorized. `IS NOT TRUE` fails closed on NULL exactly like an explicit
    'off'.
    """
    return f"""    IF ({_GUARD}) IS NOT TRUE THEN
        RAISE EXCEPTION '{fn_name}: not a platform session';
    END IF;"""


_CREATE_OPERATOR_INVITE = f"""
CREATE OR REPLACE FUNCTION platform_create_operator_invite(
    p_email text,
    p_name text,
    p_invited_by uuid
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_user_id uuid;
    v_role_id uuid;
BEGIN
{_not_platform_session_guard("platform_create_operator_invite")}

    SELECT id INTO v_role_id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN';
    IF v_role_id IS NULL THEN
        RAISE EXCEPTION 'platform_create_operator_invite: SUPER_ADMIN role not found';
    END IF;

    INSERT INTO app_user (id, tenant_id, account_type, email, name, status, invited_by)
    VALUES (gen_random_uuid(), NULL, 'platform', p_email, p_name, 'invited', p_invited_by)
    RETURNING id INTO v_user_id;

    INSERT INTO user_role (id, tenant_id, app_user_id, role_id, granted_by, granted_at)
    VALUES (gen_random_uuid(), NULL, v_user_id, v_role_id, p_invited_by, now());

    RETURN v_user_id;
END;
$$
"""

_CREATE_PASSWORD_IDENTITY = f"""
CREATE OR REPLACE FUNCTION platform_create_password_identity(
    p_app_user_id uuid,
    p_email text,
    p_hashed_password text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_identity_id uuid;
BEGIN
{_not_platform_session_guard("platform_create_password_identity")}
    IF NOT EXISTS (
        SELECT 1 FROM app_user
         WHERE id = p_app_user_id AND tenant_id IS NULL AND account_type = 'platform'
    ) THEN
        RAISE EXCEPTION 'platform_create_password_identity: no such platform operator';
    END IF;

    INSERT INTO user_identity (
        id, tenant_id, app_user_id, provider_type, provider_subject, email,
        hashed_password, mfa_enabled
    )
    VALUES (
        gen_random_uuid(), NULL, p_app_user_id, 'password', p_email, p_email,
        p_hashed_password, false
    )
    RETURNING id INTO v_identity_id;

    RETURN v_identity_id;
END;
$$
"""

_SET_OPERATOR_STATUS = f"""
CREATE OR REPLACE FUNCTION platform_set_operator_status(
    p_app_user_id uuid,
    p_status text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
BEGIN
{_not_platform_session_guard("platform_set_operator_status")}
    IF p_status NOT IN ('active', 'deactivated') THEN
        RAISE EXCEPTION 'platform_set_operator_status: invalid status %', p_status;
    END IF;

    UPDATE app_user
       SET status = p_status
     WHERE id = p_app_user_id
       AND tenant_id IS NULL
       AND account_type = 'platform';
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

_FUNCTIONS = (_CREATE_OPERATOR_INVITE, _CREATE_PASSWORD_IDENTITY, _SET_OPERATOR_STATUS)
_SIGNATURES = (
    "platform_create_operator_invite(text, text, uuid)",
    "platform_create_password_identity(uuid, text, text)",
    "platform_set_operator_status(uuid, text)",
)


def upgrade() -> None:
    # Column-scoped grants — the definer owner can only touch exactly the columns
    # each function needs, never e.g. app_user.tenant_id or user_identity.totp_seed_ct.
    op.execute(f"GRANT SELECT ON role TO {DEFINER_ROLE}")
    op.execute(f"GRANT SELECT ON app_user TO {DEFINER_ROLE}")
    op.execute(
        f"GRANT INSERT (id, tenant_id, account_type, email, name, status, invited_by) "
        f"ON app_user TO {DEFINER_ROLE}"
    )
    op.execute(f"GRANT UPDATE (status) ON app_user TO {DEFINER_ROLE}")
    op.execute(
        f"GRANT INSERT (id, tenant_id, app_user_id, role_id, granted_by, granted_at) "
        f"ON user_role TO {DEFINER_ROLE}"
    )
    op.execute(
        f"GRANT INSERT (id, tenant_id, app_user_id, provider_type, provider_subject, "
        f"email, hashed_password, mfa_enabled) ON user_identity TO {DEFINER_ROLE}"
    )
    # Postgres additionally requires SELECT on every column named in a RETURNING
    # clause, even for an INSERT the role is already allowed to perform — without
    # this, `platform_create_password_identity`'s `RETURNING id` raises "permission
    # denied for table user_identity" despite the INSERT grant above. Scoped to just
    # `id` (the only column the function returns) — never a blanket SELECT that would
    # let the definer owner read hashed_password/totp_seed_ct.
    op.execute(f"GRANT SELECT (id) ON user_identity TO {DEFINER_ROLE}")
    for fn in _FUNCTIONS:
        op.execute(fn)
    for sig in _SIGNATURES:
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER_ROLE}")
        # Postgres grants EXECUTE to PUBLIC by default; the app.platform GUC guard
        # inside each function is not a privilege check (any session can SET it), so
        # EXECUTE itself must be the boundary — mirrors 3f7a9c2e8b41's lockdown of
        # the sibling f066c667ddc1 functions.
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {_APP_ROLE}")


def downgrade() -> None:
    # NOTE: REVOKE ALL ON user_identity also revokes the SELECT/UPDATE(...) grant
    # migration f066c667ddc1 gave vera_definer_owner — if this migration is ever
    # downgraded, f066c667ddc1's functions need their grant re-applied too. Downgrade
    # here is a rare, manually-supervised path, same as elsewhere in this migration set.
    for sig in _SIGNATURES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO PUBLIC")
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute(f"REVOKE ALL ON app_user FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE ALL ON user_role FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE ALL ON user_identity FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE SELECT ON role FROM {DEFINER_ROLE}")
