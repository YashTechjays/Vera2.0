"""platform MFA enroll definer functions

Revision ID: f066c667ddc1
Revises: efa94eaaf3f9
Create Date: 2026-07-08 18:03:05.393637

Browser-based MFA enrollment for a platform operator (ADR-0006 §D) needs to WRITE
the operator's NULL-tenant `user_identity` row during login — storing the TOTP seed,
then flipping `mfa_enabled`. The platform-readable RLS policy's WITH CHECK is strict
equality, so the RLS-bound app role can never write a NULL-tenant row directly (see
db/rls.py). Mirror the sanctioned pattern from migration 0002: two narrow,
fixed-search_path SECURITY DEFINER functions owned by `vera_definer_owner` (NOLOGIN,
BYPASSRLS). Both are guarded on `tenant_id IS NULL AND mfa_enabled = false`, so they
can only ever act during the enrollment window and never overwrite an active
operator's MFA.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f066c667ddc1"
down_revision: str | None = "efa94eaaf3f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"

# Store the envelope-encrypted TOTP seed onto a not-yet-enrolled platform identity.
# Guarded so it only touches a NULL-tenant, mfa_enabled=false row — an already-enrolled
# operator's seed can never be overwritten through this path.
_STORE_SEED = f"""
CREATE OR REPLACE FUNCTION platform_store_mfa_seed(
    p_identity_id uuid,
    p_seed_ct bytea,
    p_dek_ct bytea,
    p_key_ref text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
BEGIN
    UPDATE user_identity
       SET totp_seed_ct = p_seed_ct,
           totp_dek_ct = p_dek_ct,
           totp_key_ref = p_key_ref,
           recovery_code_hashes = '[]'::jsonb
     WHERE id = p_identity_id
       AND tenant_id IS NULL
       AND mfa_enabled = false;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

# Finish enrollment: store recovery-code hashes and flip mfa_enabled. Same guard, so
# it only ever flips a not-yet-enrolled platform identity to enrolled.
_ACTIVATE = f"""
CREATE OR REPLACE FUNCTION platform_activate_mfa(
    p_identity_id uuid,
    p_recovery_hashes jsonb
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
BEGIN
    UPDATE user_identity
       SET recovery_code_hashes = p_recovery_hashes,
           mfa_enabled = true
     WHERE id = p_identity_id
       AND tenant_id IS NULL
       AND mfa_enabled = false;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

_FUNCTIONS = (_STORE_SEED, _ACTIVATE)
_SIGNATURES = (
    "platform_store_mfa_seed(uuid, bytea, bytea, text)",
    "platform_activate_mfa(uuid, jsonb)",
)


def upgrade() -> None:
    # Definer owner role already exists (migration 0002); grant it the narrow UPDATE
    # these functions need on user_identity.
    op.execute(f"GRANT SELECT, UPDATE ON user_identity TO {DEFINER_ROLE}")
    for fn in _FUNCTIONS:
        op.execute(fn)
    # Ownership by the BYPASSRLS role is what lets the functions write the NULL-tenant row.
    for sig in _SIGNATURES:
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    for sig in _SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute(f"REVOKE UPDATE ON user_identity FROM {DEFINER_ROLE}")
