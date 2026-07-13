"""grant virtual assistant live monitoring and data management perms

Revision ID: 65ab40d4f511
Revises: 3083477bf7a5
Create Date: 2026-07-13 18:00:36.951016

The VIRTUAL_ASSISTANT global system role gains access to the Live Monitoring
page (calls:read, calls:publish) and the Data Management page (forms:read,
forms:write), mirroring rbac_defaults.py. All four permission codes already
exist (no new `permission` rows) — this only grants them to VIRTUAL_ASSISTANT.

Runs on the privileged migration connection (not RLS-bound) — the strict
WITH CHECK on NULL-tenant role_permission rows does not block it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "65ab40d4f511"
down_revision: str | None = "3083477bf7a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODES = ("calls:read", "calls:publish", "forms:read", "forms:write")


def upgrade() -> None:
    for code in _PERMISSION_CODES:
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), NULL, r.id, p.id "
            "FROM role r, permission p "
            f"WHERE r.tenant_id IS NULL AND r.name = 'VIRTUAL_ASSISTANT' AND p.code = '{code}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    # Same rationale as the other permission seeds: grants are indistinguishable
    # from live product data added since — revert by hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for grant_virtual_assistant_live_monitoring_data_mgmt: "
        "cannot safely distinguish this migration's grants from live product data added since"
    )
