"""seed forms:export permission

Revision ID: 76a992faa2c5
Revises: 13b8dc4aaa83
Create Date: 2026-07-10 09:01:00.000000

Inserts the forms:export permission and grants it to the global TENANT_ADMIN and
SUPERVISOR system roles. Every export is a PHI disclosure; this permission gates
the endpoint so the action is always RBAC-checked and FORM_EXPORTED-audited.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "76a992faa2c5"
down_revision: str | None = "13b8dc4aaa83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "forms:export"
_DESCRIPTION = "Export a completed form as a file (PHI disclosure; every export is audited)"
_ROLE_NAMES = ("TENANT_ADMIN", "SUPERVISOR")


def upgrade() -> None:
    op.execute(
        "INSERT INTO permission (id, code, description) "
        f"VALUES (gen_random_uuid(), '{_PERMISSION_CODE}', '{_DESCRIPTION}') "
        "ON CONFLICT (code) DO NOTHING"
    )
    _roles = ", ".join(f"'{r}'" for r in _ROLE_NAMES)
    op.execute(
        "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
        "SELECT gen_random_uuid(), NULL, r.id, p.id FROM role r, permission p "
        f"WHERE r.tenant_id IS NULL AND r.name IN ({_roles}) "
        f"AND p.code = '{_PERMISSION_CODE}' "
        "ON CONFLICT (role_id, permission_id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permission WHERE permission_id IN "
        f"(SELECT id FROM permission WHERE code = '{_PERMISSION_CODE}')"
    )
    op.execute(f"DELETE FROM permission WHERE code = '{_PERMISSION_CODE}'")
