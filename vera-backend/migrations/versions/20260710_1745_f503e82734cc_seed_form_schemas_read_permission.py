"""seed platform:form_schemas:read permission and grant it to SUPER_ADMIN

Revision ID: f503e82734cc
Revises: 089b3e98f0b0
Create Date: 2026-07-10 17:45:00.000000

The Super Admin screen gains a read-only Form Schemas catalog view, gated by a
dedicated platform:form_schemas:read permission (api/v1/form_schemas.py). This
seeds the permission and grants it to the global SUPER_ADMIN role, mirroring
rbac_defaults.py. No backfill: it is a new capability, not a rename.

Runs on the privileged migration connection (not RLS-bound) — the strict
WITH CHECK on NULL-tenant role_permission rows does not block it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f503e82734cc"
down_revision: str | None = "089b3e98f0b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "platform:form_schemas:read"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permission (id, code, description) "
        f"VALUES (gen_random_uuid(), '{_PERMISSION_CODE}', "
        "'View form schemas and their versions') "
        "ON CONFLICT (code) DO NOTHING"
    )
    op.execute(
        "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
        "SELECT gen_random_uuid(), NULL, r.id, p.id "
        "FROM role r, permission p "
        f"WHERE r.tenant_id IS NULL AND r.name = 'SUPER_ADMIN' "
        f"AND p.code = '{_PERMISSION_CODE}' "
        "ON CONFLICT (role_id, permission_id) DO NOTHING"
    )


def downgrade() -> None:
    # Same rationale as the other permission seeds: grants are indistinguishable
    # from live product data added since — revert by hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_form_schemas_read_permission: cannot safely "
        "distinguish this migration's grants from live product data added since"
    )
