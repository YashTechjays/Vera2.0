"""tenant observer_enabled column + platform write definer fn

Revision ID: 59308656acda
Revises: 9cec58e69e92
Create Date: 2026-07-23 15:20:00.000000

Adds the per-tenant AI form-filling (observer) master switch and the sanctioned
write path a super-admin uses to flip it.

`tenant.observer_enabled` is added idempotently (`ADD COLUMN IF NOT EXISTS`): the CI
gate runs `create_all` from 0001 on a fresh DB, which already materializes the column
from the model, so a raw `add_column` would collide (repo CLAUDE.md).

The write path mirrors the platform-operator lifecycle definer (migration d226261a20ca):
the tenant table's platform-readable RLS policy (0022) is SELECT-only, so an RLS-bound
platform session can never UPDATE a tenant row directly. A narrow, fixed-search_path
SECURITY DEFINER function owned by vera_definer_owner performs the write, guarded by
`current_setting('app.platform', true) = 'on'` read as `IS NOT TRUE` (fail-closed on the
NULL an ordinary tenant session yields). The GUC is not the privilege boundary — any
session can SET it — so EXECUTE is revoked from PUBLIC and granted only to the deployed
app role, exactly as migration d226261a20ca / 3f8ecb6efb86 do.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "59308656acda"
down_revision: str | None = "9cec58e69e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"
_SIGNATURE = "platform_set_tenant_observer_enabled(uuid, boolean)"

_SET_TENANT_OBSERVER = """
CREATE OR REPLACE FUNCTION platform_set_tenant_observer_enabled(
    p_tenant_id uuid,
    p_enabled boolean
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_count bigint;
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_set_tenant_observer_enabled: not a platform session';
    END IF;

    UPDATE tenant
       SET observer_enabled = p_enabled
     WHERE id = p_tenant_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS observer_enabled boolean NOT NULL DEFAULT true"
    )
    # Column-scoped grants: the definer owner may read only `id` (needed by the WHERE)
    # and write only `observer_enabled` — never any other tenant column.
    op.execute(f"GRANT SELECT (id) ON tenant TO {DEFINER_ROLE}")
    op.execute(f"GRANT UPDATE (observer_enabled) ON tenant TO {DEFINER_ROLE}")
    op.execute(_SET_TENANT_OBSERVER)
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO {DEFINER_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM {_APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO PUBLIC")
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
    op.execute(f"REVOKE UPDATE (observer_enabled) ON tenant FROM {DEFINER_ROLE}")
    op.execute(f"REVOKE SELECT (id) ON tenant FROM {DEFINER_ROLE}")
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS observer_enabled")
