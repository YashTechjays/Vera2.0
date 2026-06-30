"""Drop the legacy twilio_sip integration_type (renamed to livekit_outbound_trunk_id)

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-29

scripts/seed.py renamed the outbound-trunk catalog type from `twilio_sip` to
`livekit_outbound_trunk_id`. The seeder upserts by `name`, so the old row would
linger on any already-seeded DB. Pre-launch, no tenant has configured it; delete any
dependent `integration` rows first (FK is ondelete=RESTRICT) and then the type row.
Idempotent: a no-op when the row is already absent.

Irreversible for tenant data: downgrade re-creates only the empty catalog row, not any
deleted tenant credentials.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM integration
        WHERE integration_type_id IN (
            SELECT id FROM integration_type WHERE name = 'twilio_sip'
        )
        """
    )
    op.execute("DELETE FROM integration_type WHERE name = 'twilio_sip'")


def downgrade() -> None:
    # Best-effort structural restore of the catalog row only; created_at/updated_at fill
    # from their server_default, and the id is arbitrary (Python-side default in the model).
    op.execute(
        """
        INSERT INTO integration_type (id, name, credentials_schema)
        VALUES (gen_random_uuid(), 'twilio_sip', '{"twilio_sip_trunk": "string"}'::jsonb)
        ON CONFLICT (name) DO NOTHING
        """
    )
