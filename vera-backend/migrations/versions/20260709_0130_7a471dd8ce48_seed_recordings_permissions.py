"""seed recordings permissions

Revision ID: 7a471dd8ce48
Revises: 34124f06dc31
Create Date: 2026-07-09 01:30:43.375264

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7a471dd8ce48"
down_revision: str | None = "34124f06dc31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMS = {
    "recordings:read": "Play back call recordings (every playback is audited)",
    "recordings:manage": "Manage the tenant's recording retention policy",
}
# Which seeded system roles get which new permission.
_GRANTS = {
    "TENANT_ADMIN": ("recordings:read", "recordings:manage"),
    "SUPERVISOR": ("recordings:read",),
    "SUPER_ADMIN": ("recordings:read", "recordings:manage"),
}


def upgrade() -> None:
    for code, description in _PERMS.items():
        # Escape single quotes in the description by doubling them
        escaped_desc = description.replace("'", "''")
        op.execute(
            "INSERT INTO permission (id, code, description) "
            f"VALUES (gen_random_uuid(), '{code}', '{escaped_desc}') "
            "ON CONFLICT (code) DO NOTHING"
        )
    for role_name, codes in _GRANTS.items():
        for code in codes:
            op.execute(
                "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
                "SELECT gen_random_uuid(), NULL, r.id, p.id "
                "FROM role r, permission p "
                f"WHERE r.tenant_id IS NULL AND r.name = '{role_name}' "
                f"AND p.code = '{code}' "
                "ON CONFLICT (role_id, permission_id) DO NOTHING"
            )


def downgrade() -> None:
    # Same rationale as 25e54e43fcf3: seeded grants are indistinguishable from
    # live product grants added since — never bulk-delete by permission code.
    raise RuntimeError(
        "downgrade unsupported for seed_recordings_permissions — revert by hand "
        "after confirming no live grants exist (see 25e54e43fcf3 for rationale)"
    )
