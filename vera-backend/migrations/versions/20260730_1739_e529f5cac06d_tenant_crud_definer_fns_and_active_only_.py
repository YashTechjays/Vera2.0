"""tenant crud definer fns and active only slug resolver

Revision ID: e529f5cac06d
Revises: 22683a464337
Create Date: 2026-07-30 17:39:02.324733

The sanctioned write paths a platform operator uses to create, edit, and
deactivate/reactivate a tenant (VR2-30), plus the login change that makes
deactivation actually block sign-in.

Same shape as 59308656acda / 9de48c83deeb: the tenant table's platform-readable RLS
policy (0022) is SELECT-only, so an RLS-bound platform session can never write a tenant
row directly. Narrow, fixed-search_path SECURITY DEFINER functions owned by
vera_definer_owner perform the writes, each guarded by
`current_setting('app.platform', true) = 'on'` read as `IS NOT TRUE` (fail-closed on the
NULL an ordinary tenant session yields). The GUC is not the privilege boundary — any
session can SET it — so EXECUTE is revoked from PUBLIC and granted only to the deployed
app role.

`resolve_tenant_by_slug` gains `AND status = 'active'`. That single clause is what makes
deactivation meaningful: login resolves the tenant by slug through this function, so a
deactivated tenant's slug stops resolving and login returns the uniform 401 with no hint
the tenant exists. Return type and parameter type are unchanged, so CREATE OR REPLACE is
safe (a param-type change would need DROP + recreate + re-ALTER OWNER, per repo
CLAUDE.md); ownership is re-asserted anyway.

Deliberately NOT writable through these functions: `slug` (immutable — it is in the login
URL, and it is absent from the UPDATE column grants so the database enforces that too),
`gcip_tenant_id` (identity-provider plumbing) and `persona_tweak` (the tenant's own config
surface). The update function takes NULL for "leave unchanged" on every field, EXCEPT the
two nullable columns (`region`, `recording_retention_days`) where NULL is itself a valid
value ("no region set" / "no retention limit") — those two each get their own boolean
"clear it" flag so "leave unchanged" and "set to NULL" stay distinguishable.

Two things this migration also has to fix before `platform_create_tenant` can work at all:

- `max_agents_per_va`, `max_retries`, `queue_expiry_hours`, `persona_tweak`,
  `observer_enabled` and `auto_retry_enabled` are all NOT NULL but had **no database
  default** — their defaults lived only as SQLAlchemy Python-side `default=`, which
  `create_all` does not translate into DDL. `retry_fill_threshold` and
  `max_concurrent_calls` are the only siblings that already carry server defaults; this
  migration brings the other six in line so an INSERT naming only the identity columns
  succeeds. Adding a DEFAULT never rewrites existing rows.
- `id` has no server default either, and the project's ids are **UUIDv7** (ADR-0002) minted
  by `vera_core.db.uuid7`. `gen_random_uuid()` would emit a v4 and break that ordering, so
  the caller passes the id in rather than the function generating one.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "e529f5cac06d"
down_revision: str | None = "22683a464337"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"

_CREATE_SIG = "platform_create_tenant(uuid, text, text, text, uuid)"
_UPDATE_SIG = (
    "platform_update_tenant(uuid, text, text, boolean, boolean, boolean, numeric,"
    " integer, integer, integer, integer, integer, boolean)"
)
_STATUS_SIG = "platform_set_tenant_status(uuid, text)"
_RESOLVE_SIG = "resolve_tenant_by_slug(text)"

# The caller supplies both UUIDv7 ids (ADR-0002). Every other tenant column falls to its
# server default. A duplicate slug surfaces as the tenant slug unique violation, which the
# router maps to 409 — that constraint is the durable de-dup, not a pre-check here.
#
# A tenant with no enabled login provider can never be signed into, so creation also opens
# a default password provider — enabled, no enforced MFA (a super admin can tighten that
# later the same way an existing tenant's provider is edited). Not optional / not a second
# step: an operator invited into a brand-new tenant must be able to log in immediately.
_CREATE_TENANT = """
CREATE OR REPLACE FUNCTION platform_create_tenant(
    p_id uuid,
    p_name text,
    p_slug text,
    p_region text,
    p_sso_provider_id uuid
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_create_tenant: not a platform session';
    END IF;

    INSERT INTO tenant (id, name, slug, status, region)
    VALUES (p_id, p_name, p_slug, 'active', p_region);

    INSERT INTO sso_provider (id, tenant_id, provider_type, display_name, enabled, enforce_mfa)
    VALUES (p_sso_provider_id, p_id, 'password', 'Password', true, false);
END;
$$
"""

# NULL means "leave unchanged" for every field, so the router can send a partial patch.
# p_clear_region / p_clear_recording_retention_days distinguish "clear it" from "leave
# it" for the two nullable columns, where a plain NULL argument is ambiguous.
_UPDATE_TENANT = """
CREATE OR REPLACE FUNCTION platform_update_tenant(
    p_tenant_id uuid,
    p_name text,
    p_region text,
    p_clear_region boolean,
    p_observer_enabled boolean,
    p_auto_retry_enabled boolean,
    p_retry_fill_threshold numeric,
    p_max_agents_per_va integer,
    p_max_concurrent_calls integer,
    p_max_retries integer,
    p_queue_expiry_hours integer,
    p_recording_retention_days integer,
    p_clear_recording_retention_days boolean
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_count bigint;
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_update_tenant: not a platform session';
    END IF;

    UPDATE tenant
       SET name = COALESCE(p_name, name),
           region = CASE
               WHEN p_clear_region IS TRUE THEN NULL
               ELSE COALESCE(p_region, region)
           END,
           observer_enabled = COALESCE(p_observer_enabled, observer_enabled),
           auto_retry_enabled = COALESCE(p_auto_retry_enabled, auto_retry_enabled),
           retry_fill_threshold = COALESCE(p_retry_fill_threshold, retry_fill_threshold),
           max_agents_per_va = COALESCE(p_max_agents_per_va, max_agents_per_va),
           max_concurrent_calls = COALESCE(p_max_concurrent_calls, max_concurrent_calls),
           max_retries = COALESCE(p_max_retries, max_retries),
           queue_expiry_hours = COALESCE(p_queue_expiry_hours, queue_expiry_hours),
           recording_retention_days = CASE
               WHEN p_clear_recording_retention_days IS TRUE THEN NULL
               ELSE COALESCE(p_recording_retention_days, recording_retention_days)
           END
     WHERE id = p_tenant_id AND deleted_at IS NULL;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

# Returns NULL for "no such tenant" and the PREVIOUS status otherwise, so the router can
# tell a 404 from an already-in-that-state 409 in one round trip.
_SET_TENANT_STATUS = """
CREATE OR REPLACE FUNCTION platform_set_tenant_status(
    p_tenant_id uuid,
    p_status text
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_previous text;
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_set_tenant_status: not a platform session';
    END IF;
    IF p_status NOT IN ('active', 'deactivated') THEN
        RAISE EXCEPTION 'platform_set_tenant_status: unsupported status %', p_status;
    END IF;

    SELECT status INTO v_previous
      FROM tenant
     WHERE id = p_tenant_id AND deleted_at IS NULL
       FOR UPDATE;
    IF v_previous IS NULL THEN
        RETURN NULL;
    END IF;
    IF v_previous <> p_status THEN
        UPDATE tenant SET status = p_status WHERE id = p_tenant_id;
    END IF;
    RETURN v_previous;
END;
$$
"""

_RESOLVE_ACTIVE_ONLY = """
CREATE OR REPLACE FUNCTION resolve_tenant_by_slug(p_slug text) RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT id FROM tenant
    WHERE slug = p_slug AND deleted_at IS NULL AND status = 'active';
$$
"""

_RESOLVE_ANY_STATUS = """
CREATE OR REPLACE FUNCTION resolve_tenant_by_slug(p_slug text) RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT id FROM tenant WHERE slug = p_slug AND deleted_at IS NULL;
$$
"""

# Only what platform_create_tenant names in its INSERT.
_INSERT_COLUMNS = ("id", "name", "slug", "status", "region")

# The default password provider platform_create_tenant opens alongside the tenant row.
_SSO_PROVIDER_INSERT_COLUMNS = (
    "id",
    "tenant_id",
    "provider_type",
    "display_name",
    "enabled",
    "enforce_mfa",
)

# Only what platform_update_tenant / platform_set_tenant_status assign. `slug` is absent
# on purpose: its immutability is enforced by the grant, not just by the SQL text.
_UPDATE_COLUMNS = (
    "name",
    "status",
    "region",
    "observer_enabled",
    "auto_retry_enabled",
    "retry_fill_threshold",
    "max_agents_per_va",
    "max_concurrent_calls",
    "max_retries",
    "queue_expiry_hours",
    "recording_retention_days",
)

# NOT NULL columns whose default existed only as a SQLAlchemy Python-side `default=`.
# Values mirror models/tenant.py exactly; keep the six in step.
_MISSING_SERVER_DEFAULTS = (
    ("max_agents_per_va", "3"),
    ("max_retries", "5"),
    ("queue_expiry_hours", "48"),
    ("persona_tweak", "'{}'::jsonb"),
    ("observer_enabled", "true"),
    ("auto_retry_enabled", "true"),
)

_FUNCTIONS = (
    (_CREATE_TENANT, _CREATE_SIG),
    (_UPDATE_TENANT, _UPDATE_SIG),
    (_SET_TENANT_STATUS, _STATUS_SIG),
)


def upgrade() -> None:
    for column, default in _MISSING_SERVER_DEFAULTS:
        op.execute(f"ALTER TABLE tenant ALTER COLUMN {column} SET DEFAULT {default}")

    # Column-scoped grants: the definer owner reads only what the WHERE needs and writes
    # only these columns — never gcip_tenant_id, persona_tweak, or (on update) slug.
    op.execute(f"GRANT SELECT (id, status, deleted_at) ON tenant TO {DEFINER_ROLE}")
    op.execute(f"GRANT INSERT ({', '.join(_INSERT_COLUMNS)}) ON tenant TO {DEFINER_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(_UPDATE_COLUMNS)}) ON tenant TO {DEFINER_ROLE}")
    op.execute(
        f"GRANT INSERT ({', '.join(_SSO_PROVIDER_INSERT_COLUMNS)}) "
        f"ON sso_provider TO {DEFINER_ROLE}"
    )

    for body, signature in _FUNCTIONS:
        op.execute(body)
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {DEFINER_ROLE}")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")

    op.execute(_RESOLVE_ACTIVE_ONLY)
    op.execute(f"ALTER FUNCTION {_RESOLVE_SIG} OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    op.execute(_RESOLVE_ANY_STATUS)
    op.execute(f"ALTER FUNCTION {_RESOLVE_SIG} OWNER TO {DEFINER_ROLE}")

    for _, signature in _FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM {_APP_ROLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    op.execute(
        f"REVOKE INSERT ({', '.join(_SSO_PROVIDER_INSERT_COLUMNS)}) "
        f"ON sso_provider FROM {DEFINER_ROLE}"
    )
    op.execute(f"REVOKE UPDATE ({', '.join(_UPDATE_COLUMNS)}) ON tenant FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE INSERT ({', '.join(_INSERT_COLUMNS)}) ON tenant FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE SELECT (id, status, deleted_at) ON tenant FROM {DEFINER_ROLE}")

    for column, _ in _MISSING_SERVER_DEFAULTS:
        op.execute(f"ALTER TABLE tenant ALTER COLUMN {column} DROP DEFAULT")
