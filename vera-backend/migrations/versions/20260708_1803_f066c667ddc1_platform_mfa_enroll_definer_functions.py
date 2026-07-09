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
BYPASSRLS).

Guards, in every WHERE clause:
- `tenant_id IS NULL AND mfa_enabled = false` — the functions only act during the
  enrollment window and never overwrite an active operator's MFA;
- `current_setting('app.platform', true) = 'on'` — only a platform session can use
  the write path (same GUC trust model as the platform-readable RLS policies), so a
  tenant-scoped connection calling them is a no-op;
- activate additionally compares the caller's expected seed ciphertext, so a seed
  re-minted by a concurrent login can't be activated against a stale QR.

Platform MFA is TOTP-only: recovery-code consumption would need yet another definer
write path on an ENROLLED row, breaking the enrollment-window guarantee — so no
recovery codes are ever stored (`recovery_code_hashes` stays `[]`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f066c667ddc1"
down_revision: str | None = "efa94eaaf3f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"
_GUARD = """tenant_id IS NULL
       AND mfa_enabled = false
       AND current_setting('app.platform', true) = 'on'"""

# Store the envelope-encrypted TOTP seed onto a not-yet-enrolled platform identity.
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
       AND {_GUARD};
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

# Finish enrollment: flip mfa_enabled, but only if the row still holds the exact seed
# the operator scanned (compare-and-set against a concurrent re-enroll login).
_ACTIVATE = f"""
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

_FUNCTIONS = (_STORE_SEED, _ACTIVATE)
_SIGNATURES = (
    "platform_store_mfa_seed(uuid, bytea, bytea, text)",
    "platform_activate_mfa(uuid, bytea)",
)

# Columns the definer functions may write — scoped so the owner role can never
# touch e.g. hashed_password through a future function.
_UPDATE_COLUMNS = "totp_seed_ct, totp_dek_ct, totp_key_ref, recovery_code_hashes, mfa_enabled"


def upgrade() -> None:
    # Drop the pre-review (uuid, jsonb) overload on DBs that applied the original
    # revision of this migration — CREATE OR REPLACE would leave it behind.
    op.execute("DROP FUNCTION IF EXISTS platform_activate_mfa(uuid, jsonb)")
    # Definer owner role already exists (migration 0002); grant it the narrow access
    # these functions need on user_identity (SELECT for the WHERE, column-scoped UPDATE).
    op.execute(f"GRANT SELECT, UPDATE ({_UPDATE_COLUMNS}) ON user_identity TO {DEFINER_ROLE}")
    for fn in _FUNCTIONS:
        op.execute(fn)
    # Ownership by the BYPASSRLS role is what lets the functions write the NULL-tenant row.
    for sig in _SIGNATURES:
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    for sig in _SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute(f"REVOKE SELECT, UPDATE ON user_identity FROM {DEFINER_ROLE}")
