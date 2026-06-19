"""elevation expiry computed server-side — duration in, expires_at from DB clock

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17

`create_elevation_grant` previously took a precomputed `expires_at timestamptz`,
which the control plane derived from its OWN clock (`datetime.now(UTC) + ...`).
That made `tenant_elevation.expires_at` the one elevation timestamp not minted by
the database. This migration moves the computation into the function: it now takes
`p_duration_minutes integer` and sets `expires_at = now() + interval` so EVERY
elevation timestamp (`granted_at`, `expires_at`, `ended_at`) comes from the single
DB clock — the NTP-synced source of truth (HIPAA audit-trail integrity).

The 4th parameter's type changes (timestamptz → integer), so the old overload is
DROPped, not just CREATE-OR-REPLACEd — otherwise both would coexist. Ownership is
reassigned to the definer role so BYPASSRLS still applies (see 0002)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"

# Same fixed search_path discipline as 0002 — stops a caller from shadowing
# tenant_elevation / now() / make_interval on a search_path they control.
_SEARCH_PATH = "SET search_path = pg_catalog, public"

# New signature: duration in minutes, expiry computed from the DB clock.
CREATE_ELEVATION_GRANT_MINUTES = f"""
CREATE OR REPLACE FUNCTION create_elevation_grant(
    p_super_admin_user_id uuid,
    p_target_tenant_id uuid,
    p_reason text,
    p_duration_minutes integer
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
    IF p_duration_minutes IS NULL OR p_duration_minutes <= 0 THEN
        RAISE EXCEPTION 'elevation duration must be positive' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO tenant_elevation
        (id, super_admin_user_id, target_tenant_id, reason, granted_at, expires_at, ended_at)
    VALUES
        (v_id, p_super_admin_user_id, p_target_tenant_id, p_reason,
         now(), now() + make_interval(mins => p_duration_minutes), NULL);
    RETURN v_id;
END;
$$
"""

# The 0002 original, recreated verbatim on downgrade.
CREATE_ELEVATION_GRANT_TIMESTAMPTZ = f"""
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


def upgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS create_elevation_grant(uuid, uuid, text, timestamptz)")
    op.execute(CREATE_ELEVATION_GRANT_MINUTES)
    # Ownership is what makes the SECURITY DEFINER function run with BYPASSRLS.
    op.execute(
        f"ALTER FUNCTION create_elevation_grant(uuid, uuid, text, integer) OWNER TO {DEFINER_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS create_elevation_grant(uuid, uuid, text, integer)")
    op.execute(CREATE_ELEVATION_GRANT_TIMESTAMPTZ)
    op.execute(
        "ALTER FUNCTION create_elevation_grant(uuid, uuid, text, timestamptz) "
        f"OWNER TO {DEFINER_ROLE}"
    )
