"""tenant auto retry config

Revision ID: 9de48c83deeb
Revises: 2435e03793ff
Create Date: 2026-07-29 14:47:36.123671

Adds the per-tenant auto-retry master switch and lowers the fill-%
retry threshold's default from the never-admin-settable 0.95 to 0.50,
backfilling any tenant row still sitting at that untouched default.

`tenant.auto_retry_enabled` is added idempotently (`ADD COLUMN IF NOT EXISTS`): the
CI gate runs `create_all` from 0001 on a fresh DB, which already materializes the
column from the model, so a raw `add_column` would collide (repo CLAUDE.md).

The write path mirrors the platform-observer-toggle definer (migration
59308656acda): the tenant table's platform-readable RLS policy (0022) is
SELECT-only, so an RLS-bound platform session can never UPDATE a tenant row
directly. A narrow, fixed-search_path SECURITY DEFINER function owned by
vera_definer_owner performs the write, guarded by
`current_setting('app.platform', true) = 'on'` read as `IS NOT TRUE` (fail-closed on
the NULL an ordinary tenant session yields). The GUC is not the privilege boundary —
any session can SET it — so EXECUTE is revoked from PUBLIC and granted only to the
deployed app role, exactly as migration 59308656acda / d226261a20ca / 3f8ecb6efb86 do.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "9de48c83deeb"
down_revision: str | None = "2435e03793ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"
_SIGNATURE = "platform_set_tenant_retry_config(uuid, boolean, numeric)"

# 0.95 = the never-admin-settable old default (no API/UI ever wrote this column),
# so equality identifies untouched rows; deliberately-set values are left alone.
BACKFILL_THRESHOLD = (
    "UPDATE tenant SET retry_fill_threshold = 0.50 WHERE retry_fill_threshold = 0.95"
)

_SET_TENANT_RETRY_CONFIG = """
CREATE OR REPLACE FUNCTION platform_set_tenant_retry_config(
    p_tenant_id uuid,
    p_enabled boolean,
    p_threshold numeric
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_count bigint;
BEGIN
    IF (current_setting('app.platform', true) = 'on') IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_set_tenant_retry_config: not a platform session';
    END IF;

    -- NULL params mean "leave unchanged": partial update inside the fn, no
    -- read-merge race between a SELECT and a separate write.
    UPDATE tenant
       SET auto_retry_enabled = COALESCE(p_enabled, auto_retry_enabled),
           retry_fill_threshold = COALESCE(p_threshold, retry_fill_threshold)
     WHERE id = p_tenant_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS auto_retry_enabled"
        " boolean NOT NULL DEFAULT true"
    )
    op.execute("ALTER TABLE tenant ALTER COLUMN retry_fill_threshold SET DEFAULT 0.50")
    op.execute(BACKFILL_THRESHOLD)
    # The definer fn below is EXECUTE-granted role-wide with no in-fn bounds check,
    # so the DB CHECK is the real 0..1 guard (fresh CI gets it via 0001's create_all).
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE tenant ADD CONSTRAINT ck_tenant_retry_fill_threshold_range
                CHECK (retry_fill_threshold BETWEEN 0 AND 1);
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """
    )
    op.execute(f"GRANT SELECT (id) ON tenant TO {DEFINER_ROLE}")
    op.execute(
        f"GRANT UPDATE (auto_retry_enabled, retry_fill_threshold) ON tenant TO {DEFINER_ROLE}"
    )
    op.execute(_SET_TENANT_RETRY_CONFIG)
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO {DEFINER_ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM {_APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO PUBLIC")
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
    op.execute(
        f"REVOKE UPDATE (auto_retry_enabled, retry_fill_threshold) ON tenant FROM {DEFINER_ROLE}"
    )
    # SELECT (id) is deliberately not revoked — 59308656acda's observer fn shares it;
    # grants aren't reference-counted.
    op.execute("ALTER TABLE tenant DROP CONSTRAINT IF EXISTS ck_tenant_retry_fill_threshold_range")
    op.execute("ALTER TABLE tenant ALTER COLUMN retry_fill_threshold SET DEFAULT 0.95")
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS auto_retry_enabled")
