"""seed platform users invite and read permissions

Revision ID: 3876c58097c2
Revises: e05205e0a173
Create Date: 2026-07-22 18:29:11.841480

Platform operators gain a Platform Operators screen to invite, list, and deactivate
other operators, gated by two new permissions. Seeds them and grants both to the
global SUPER_ADMIN role, mirroring rbac_defaults.py. No backfill: new capability,
not a rename.

Runs on the privileged migration connection (not RLS-bound) — the strict WITH CHECK
on NULL-tenant role_permission rows does not block it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3876c58097c2"
down_revision: str | None = "e05205e0a173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS = {
    "platform:users:invite": "Invite, resend invitations to, and deactivate platform operators",
    "platform:users:read": "View platform operators",
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
    # Same rationale as every prior permission seed migration (e.g. f503e82734cc):
    # grants are indistinguishable from live product data added since — revert by
    # hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_platform_users_permissions: cannot safely "
        "distinguish this migration's grants from live product data added since"
    )
