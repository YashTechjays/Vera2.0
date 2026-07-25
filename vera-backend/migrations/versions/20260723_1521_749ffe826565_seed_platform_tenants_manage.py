"""seed platform:tenants:manage permission

Revision ID: 749ffe826565
Revises: 59308656acda
Create Date: 2026-07-23 15:21:00.000000

Super-admins gain a Platform Settings screen to toggle each tenant's AI form-filling
(observer) feature, gated by a new permission. Seeds it and grants it to the global
SUPER_ADMIN role, mirroring rbac_defaults.py (PLATFORM_PERMISSIONS). No backfill: new
capability, not a rename. Runs on the privileged migration connection (not RLS-bound),
so the strict WITH CHECK on NULL-tenant role_permission rows does not block it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "749ffe826565"
down_revision: str | None = "59308656acda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS = {
    "platform:tenants:manage": "View tenants and toggle their AI form-filling (observer) feature",
}


def upgrade() -> None:
    for code, description in _PERMISSIONS.items():
        op.execute(
            "INSERT INTO permission (id, code, description) "
            f"VALUES (gen_random_uuid(), '{code}', '{description}') "
            "ON CONFLICT (code) DO NOTHING"
        )
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), NULL, r.id, p.id "
            "FROM role r, permission p "
            "WHERE r.tenant_id IS NULL AND r.name = 'SUPER_ADMIN' "
            f"AND p.code = '{code}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    # Same rationale as every prior permission seed migration (e.g. 3876c58097c2): grants
    # are indistinguishable from live product data added since — revert by hand if needed.
    raise RuntimeError(
        "downgrade unsupported for seed_platform_tenants_manage: cannot safely distinguish "
        "this migration's grants from live product data added since"
    )
