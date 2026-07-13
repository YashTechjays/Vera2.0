"""seed calls:intervene permission and grant it to TENANT_ADMIN and SUPERVISOR

Revision ID: 107da9e93469
Revises: 3083477bf7a5
Create Date: 2026-07-10 19:00:00.000000

Intervening (publishing audio into a live call) moves off the implicit
calls:read gate onto a dedicated calls:intervene permission, enforced by
GET /calls/{id}/join-token?intervene=true. This seeds the permission and
grants it to the global TENANT_ADMIN and SUPERVISOR roles, mirroring
rbac_defaults.py. No backfill: intervening is a deliberate new capability
grant, not an access-preserving rename.

Runs on the privileged migration connection (not RLS-bound) — the strict
WITH CHECK on NULL-tenant role_permission rows does not block it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "107da9e93469"
down_revision: str | None = "3083477bf7a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "calls:intervene"
_GRANTEE_ROLES = ("TENANT_ADMIN", "SUPERVISOR")


def upgrade() -> None:
    op.execute(
        "INSERT INTO permission (id, code, description) "
        f"VALUES (gen_random_uuid(), '{_PERMISSION_CODE}', "
        "'Speak into a live call (publish audio) while supervising') "
        "ON CONFLICT (code) DO NOTHING"
    )
    for role_name in _GRANTEE_ROLES:
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), NULL, r.id, p.id "
            "FROM role r, permission p "
            f"WHERE r.tenant_id IS NULL AND r.name = '{role_name}' "
            f"AND p.code = '{_PERMISSION_CODE}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    # Same rationale as the voice_lab:sandbox seed: seeded grants are
    # indistinguishable at the row level from grants added since by real
    # product usage — revert by hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_calls_intervene_permission: cannot safely "
        "distinguish this migration's grants from live product data added since"
    )
