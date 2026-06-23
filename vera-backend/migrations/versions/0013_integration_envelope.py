# migrations/versions/0013_integration_envelope.py
"""Integration outbound credentials move to in-DB envelope encryption.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-23

Previously `integration.secret_ref` pointed at Google Secret Manager. The
credential is now envelope-encrypted directly on the row (same scheme as the MFA
seed, see 0009), so two ciphertext columns are added and `secret_ref` is reused
as the KMS key-version reference:

  credential_ct   bytea     AES-256-GCM ciphertext of the credential JSON
  dek_ct          bytea     KMS-wrapped Data Encryption Key
  secret_ref      varchar   (existing) now holds the KMS key-version reference

Pre-launch with no data: columns are added nullable, no backfill. Plain
`ADD COLUMN IF NOT EXISTS` per the 0008/0009 pattern.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE integration ADD COLUMN IF NOT EXISTS credential_ct bytea")
    op.execute("ALTER TABLE integration ADD COLUMN IF NOT EXISTS dek_ct bytea")


def downgrade() -> None:
    op.execute("ALTER TABLE integration DROP COLUMN IF EXISTS dek_ct")
    op.execute("ALTER TABLE integration DROP COLUMN IF EXISTS credential_ct")
