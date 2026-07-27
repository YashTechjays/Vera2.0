"""grant calls:intervene to VIRTUAL_ASSISTANT (VR2-77)

Revision ID: 738e38d86bdb
Revises: 919535223bcc
Create Date: 2026-07-24 17:20:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "738e38d86bdb"
down_revision: str | None = "9cec58e69e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The permission row exists since 107da9e93469; this only adds the VA grant.
    op.execute(
        "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
        "SELECT gen_random_uuid(), NULL, r.id, p.id "
        "FROM role r, permission p "
        "WHERE r.tenant_id IS NULL AND r.name = 'VIRTUAL_ASSISTANT' "
        "AND p.code = 'calls:intervene' "
        "ON CONFLICT (role_id, permission_id) DO NOTHING"
    )


def downgrade() -> None:
    # Seeded grants are row-level indistinguishable from grants added since by
    # real product usage — revert by hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for grant_calls_intervene_to_virtual_assistant: cannot "
        "safely distinguish this migration's grant from live product data added since"
    )
