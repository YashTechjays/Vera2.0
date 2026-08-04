"""seed reports:dashboard permission and grant to SUPER_ADMIN/TENANT_ADMIN/SUPERVISOR

Revision ID: 19f852dabb3c
Revises: 952630394b76
Create Date: 2026-08-04 20:52:00.000000

The new analytics dashboard (live panel + history report) needs its own permission.
VAs are excluded (they reach Live Monitoring via calls:read); a tenant admin can
grant more via role editing.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "19f852dabb3c"
down_revision: str | None = "952630394b76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_STATEMENTS: tuple[str, ...] = (
    """
    INSERT INTO permission (id, code, description)
    VALUES (gen_random_uuid(), 'reports:dashboard',
            'View the analytics dashboard (live panel and history report)')
    ON CONFLICT (code) DO NOTHING
    """,
    """
    INSERT INTO role_permission (id, tenant_id, role_id, permission_id)
    SELECT gen_random_uuid(), NULL, r.id, p.id
    FROM role r, permission p
    WHERE r.tenant_id IS NULL
      AND r.name IN ('SUPER_ADMIN', 'TENANT_ADMIN', 'SUPERVISOR')
      AND p.code = 'reports:dashboard'
    ON CONFLICT (role_id, permission_id) DO NOTHING
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Same rationale as the other permission seeds: grants are indistinguishable from
    # live product data added since — revert by hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_reports_dashboard_permission: cannot safely "
        "distinguish this migration's grants from live product data added since"
    )
