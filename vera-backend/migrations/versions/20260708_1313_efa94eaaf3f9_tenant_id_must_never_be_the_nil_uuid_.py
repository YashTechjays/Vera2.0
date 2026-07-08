"""tenant id must never be the nil uuid sentinel

Revision ID: efa94eaaf3f9
Revises: fb43bdd169b2
Create Date: 2026-07-08 13:13:10.104501

platform_session pins the tenant GUC to UUID(int=0) so strict RLS WITH CHECK
denies unless a row's tenant_id equals the nil UUID. That is only safe while
no tenant row ever has that id — previously guaranteed by a comment alone.
Enforce it in the DB: a seeded/fixture nil tenant now fails loudly instead of
silently granting platform sessions access to that tenant's rows.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "efa94eaaf3f9"
down_revision: str | None = "fb43bdd169b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_tenant_id_not_nil"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE tenant ADD CONSTRAINT {_CONSTRAINT}"
        " CHECK (id <> '00000000-0000-0000-0000-000000000000'::uuid)"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE tenant DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
