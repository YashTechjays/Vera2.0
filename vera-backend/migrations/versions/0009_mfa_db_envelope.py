# migrations/versions/0009_mfa_db_envelope.py
"""MFA material moves from WritableSecretProvider into the DB with envelope encryption.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-19

Replaces the opaque `mfa_secret_ref` pointer (which referenced an external
secret store) with four columns that hold the MFA material directly:

  totp_seed_ct         bytea   AES-256-GCM ciphertext of the base32 TOTP seed
  totp_dek_ct          bytea   KMS-wrapped Data Encryption Key
  totp_key_ref         varchar KMS key version reference (for rotation/audit)
  recovery_code_hashes jsonb   Array of bcrypt hashes of unused recovery codes

Pre-launch with no data: columns are added NOT NULL-free (nullable), no
backfill required. A plain `ADD COLUMN IF NOT EXISTS` is the sanctioned
pattern (see 0008 module docstring).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS mfa_secret_ref")
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS totp_seed_ct bytea")
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS totp_dek_ct bytea")
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS totp_key_ref varchar(512)")
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS recovery_code_hashes jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS recovery_code_hashes")
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS totp_key_ref")
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS totp_dek_ct")
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS totp_seed_ct")
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS mfa_secret_ref varchar(512)")
