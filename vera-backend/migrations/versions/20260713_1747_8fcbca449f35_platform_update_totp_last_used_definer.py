"""platform_update_totp_last_used definer

Revision ID: 8fcbca449f35
Revises: bec7cbf6fed0
Create Date: 2026-07-13 17:47:47.852685

Add a narrow SECURITY DEFINER function that writes `totp_last_used_timestep` on a
NULL-tenant platform identity row. The RLS-bound app role cannot UPDATE NULL-tenant rows
directly (platform_session WITH CHECK is strict equality on tenant_id), so replay
protection for platform operators requires the same definer pattern used by
platform_store_mfa_seed / platform_activate_mfa (migration f066c667ddc1).

Guards (identical pattern to the enroll definers):
- `tenant_id IS NULL` — only acts on NULL-tenant (platform) rows.
- `mfa_enabled = true` — only updates already-enrolled identities (no-op on unenrolled).
- `current_setting('app.platform', true) = 'on'` — only callable from a platform session.
- `totp_last_used_timestep IS NULL OR totp_last_used_timestep < p_step` — monotonicity:
  never allows a step to regress (handles duplicate or out-of-order calls safely).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8fcbca449f35"
down_revision: str | None = "bec7cbf6fed0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"

_UPDATE_TOTP_LAST_USED = """
CREATE OR REPLACE FUNCTION platform_update_totp_last_used(
    p_identity_id uuid,
    p_step bigint
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    UPDATE user_identity
       SET totp_last_used_timestep = p_step
     WHERE id = p_identity_id
       AND tenant_id IS NULL
       AND mfa_enabled = true
       AND current_setting('app.platform', true) = 'on'
       AND (totp_last_used_timestep IS NULL OR totp_last_used_timestep < p_step);
END;
$$
"""


def upgrade() -> None:
    # Grant the definer owner role write access to the new column; all other columns
    # on user_identity are already granted by migration f066c667ddc1.
    op.execute(f"GRANT UPDATE (totp_last_used_timestep) ON user_identity TO {DEFINER_ROLE}")
    op.execute(_UPDATE_TOTP_LAST_USED)
    op.execute(
        f"ALTER FUNCTION platform_update_totp_last_used(uuid, bigint) OWNER TO {DEFINER_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS platform_update_totp_last_used(uuid, bigint)")
    op.execute(f"REVOKE UPDATE (totp_last_used_timestep) ON user_identity FROM {DEFINER_ROLE}")
