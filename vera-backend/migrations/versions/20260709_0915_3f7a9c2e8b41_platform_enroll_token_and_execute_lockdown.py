"""platform enroll token + definer EXECUTE lockdown

Security hardening for browser-based platform MFA enrollment (PR #68 review):

1. **One-time enroll token.** Bootstrap now leaves the super-admin unenrolled, so the
   bootstrap password alone could bind an attacker's authenticator before the real
   operator's first login. Add `user_identity.enroll_token_hash`: a bcrypt-hashed secret
   set at bootstrap and required at `/platform/login` while unenrolled. `platform_activate_mfa`
   clears it on success (one-time), so the old "you also need a bootstrap secret" guarantee
   is restored — the terminal QR is replaced by this token.

2. **EXECUTE lockdown.** Postgres grants EXECUTE to PUBLIC on new functions by default, so the
   two `f066c667ddc1` definer functions were callable by any DB principal (their only runtime
   guard is the self-set `app.platform` GUC — not a barrier against a role that can run SQL).
   Revoke PUBLIC and grant EXECUTE only to the deployed app role (`$VERA_APP_DB_ROLE`, unset →
   `CURRENT_USER`). Advances devops-todo #12 for the platform-MFA pair.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "3f7a9c2e8b41"
down_revision: str | None = "f066c667ddc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"
_GUARD = """tenant_id IS NULL
       AND mfa_enabled = false
       AND current_setting('app.platform', true) = 'on'"""

# Deployment-specific app role that may EXECUTE the definer functions (prod: the Cloud SQL
# app role; tests: vera_rls_test). Templated at deploy time; unset (local/CI, where migrations
# run as the app user) → CURRENT_USER.
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"

_SIGNATURES = (
    "platform_store_mfa_seed(uuid, bytea, bytea, text)",
    "platform_activate_mfa(uuid, bytea)",
)

# platform_activate_mfa, re-created to also clear the one-time enroll token on success.
_ACTIVATE_WITH_TOKEN_CLEAR = f"""
CREATE OR REPLACE FUNCTION platform_activate_mfa(
    p_identity_id uuid,
    p_expected_seed_ct bytea
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
BEGIN
    UPDATE user_identity
       SET mfa_enabled = true,
           enroll_token_hash = NULL
     WHERE id = p_identity_id
       AND totp_seed_ct = p_expected_seed_ct
       AND {_GUARD};
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

# Prior body (no token clear) — used on downgrade.
_ACTIVATE_ORIGINAL = f"""
CREATE OR REPLACE FUNCTION platform_activate_mfa(
    p_identity_id uuid,
    p_expected_seed_ct bytea
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
BEGIN
    UPDATE user_identity
       SET mfa_enabled = true
     WHERE id = p_identity_id
       AND totp_seed_ct = p_expected_seed_ct
       AND {_GUARD};
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""


def upgrade() -> None:
    # 0001 create_all may have already made the column on a fresh DB — stay idempotent.
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS enroll_token_hash varchar(255)")
    # Let the definer own the extra column it now writes (NULL on activation).
    op.execute(f"GRANT UPDATE (enroll_token_hash) ON user_identity TO {DEFINER_ROLE}")
    op.execute(_ACTIVATE_WITH_TOKEN_CLEAR)
    # CREATE OR REPLACE keeps the same-signature owner, but re-assert it defensively.
    op.execute(f"ALTER FUNCTION {_SIGNATURES[1]} OWNER TO {DEFINER_ROLE}")
    # Take the default PUBLIC EXECUTE away from both functions; grant only the app role.
    for sig in _SIGNATURES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {_APP_ROLE}")


def downgrade() -> None:
    for sig in _SIGNATURES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO PUBLIC")
    op.execute(_ACTIVATE_ORIGINAL)
    op.execute(f"ALTER FUNCTION {_SIGNATURES[1]} OWNER TO {DEFINER_ROLE}")
    op.execute(f"REVOKE UPDATE (enroll_token_hash) ON user_identity FROM {DEFINER_ROLE}")
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS enroll_token_hash")
