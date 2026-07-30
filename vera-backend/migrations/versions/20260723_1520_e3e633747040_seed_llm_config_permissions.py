"""seed platform:llm_config:read/write permissions and grant to SUPER_ADMIN

The new Super Admin "Voice Model" page needs its own platform permissions, gating
api/v1/llm_config.py. Mirrors f503e82734cc_seed_form_schemas_read_permission.py — two
new capabilities, not a rename/backfill.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3e633747040"
down_revision: str | None = "513942e99676"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("platform:llm_config:read", "View the active voice cascade LLM model override"),
    ("platform:llm_config:write", "Set or reset the voice cascade LLM model override"),
)


def upgrade() -> None:
    for code, description in _PERMISSIONS:
        op.execute(
            "INSERT INTO permission (id, code, description) "
            f"VALUES (gen_random_uuid(), '{code}', '{description}') "
            "ON CONFLICT (code) DO NOTHING"
        )
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), NULL, r.id, p.id "
            "FROM role r, permission p "
            f"WHERE r.tenant_id IS NULL AND r.name = 'SUPER_ADMIN' AND p.code = '{code}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    # Same rationale as the other permission seeds: grants are indistinguishable from
    # live product data added since — revert by hand if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_llm_config_permissions: cannot safely "
        "distinguish this migration's grants from live product data added since"
    )
