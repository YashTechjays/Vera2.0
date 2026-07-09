"""seed insurance_providers permissions and backfill grants

Revision ID: d97ece8ca1b2
Revises: efa94eaaf3f9
Create Date: 2026-07-09 14:23:44.704498

Insurance-provider CRUD moves off the reused `platform:ivr_playbooks:*` grants onto
dedicated `platform:insurance_providers:read`/`:write` permissions. This inserts the
two new permissions and backfills each onto every role — system AND tenant-custom —
that currently holds the matching `platform:ivr_playbooks:*` permission, so nobody
(notably SUPER_ADMIN) loses access once api/v1/insurance_providers.py switches its
platform_require() gate onto the new codes.

Runs on the privileged migration connection (not RLS-bound), same as the voice_lab
seed — the strict WITH CHECK on a NULL-tenant role_permission row does not block it.
id columns are client-side defaulted (UUIDv7PKMixin) so every INSERT supplies one via
gen_random_uuid(); created_at/updated_at fall to their server_default now().
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d97ece8ca1b2"
down_revision: str | None = "efa94eaaf3f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (new permission, description, source permission whose holders it is backfilled onto)
_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    (
        "platform:insurance_providers:read",
        "View insurance providers",
        "platform:ivr_playbooks:read",
    ),
    (
        "platform:insurance_providers:write",
        "Create and manage insurance providers",
        "platform:ivr_playbooks:write",
    ),
)


def upgrade() -> None:
    for code, description, source in _PERMISSIONS:
        op.execute(
            "INSERT INTO permission (id, code, description) "
            f"VALUES (gen_random_uuid(), '{code}', '{description}') "
            "ON CONFLICT (code) DO NOTHING"
        )
        # Backfill: every role (system or tenant-custom) currently holding the source
        # ivr_playbooks permission also gets the new insurance_providers permission, so
        # existing access survives the router's switch onto the dedicated code.
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), rp.tenant_id, rp.role_id, p_new.id "
            "FROM role_permission rp "
            f"JOIN permission p_old ON p_old.id = rp.permission_id AND p_old.code = '{source}' "
            f"JOIN permission p_new ON p_new.code = '{code}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    # Deliberately NOT reversing the data this migration seeded: by the time anyone
    # downgrades, the backfilled role_permission rows are indistinguishable from grants
    # added since by real product usage (e.g. a tenant admin creating a custom role with
    # this permission). Blindly deleting by permission code would silently destroy that
    # live data. Revert by hand on an environment with no real usage since, if truly needed.
    raise RuntimeError(
        "downgrade unsupported for seed_insurance_providers_permissions: cannot safely "
        "distinguish this migration's backfilled grants from live product data added "
        "since (see comment above) — revert by hand if truly needed"
    )
