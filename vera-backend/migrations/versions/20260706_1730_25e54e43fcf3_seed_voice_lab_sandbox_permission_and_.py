"""seed voice_lab:sandbox permission, VIRTUAL_ASSISTANT role, and backfill existing
calls:read roles

Revision ID: 25e54e43fcf3
Revises: 5d7bc8c2f5ca
Create Date: 2026-07-06 17:30:20.850682

Voice Lab moves off the reused `calls:read` permission onto a dedicated
`voice_lab:sandbox` permission (docs/superpowers/specs/2026-07-06-virtual-assistant-role-design.md).
This inserts the new permission + the global VIRTUAL_ASSISTANT role (holding only
voice_lab:sandbox), and backfills voice_lab:sandbox onto every role — system AND
tenant-custom — that currently holds calls:read, so nobody loses Voice Lab access
once api/v1/voice_lab.py switches its require() gate off calls:read.

Runs on the privileged migration connection (not RLS-bound), same as 0011's provider
seed — the strict WITH CHECK on a NULL-tenant role/role_permission row does not block
it. id columns are client-side defaulted (UUIDv7PKMixin) so every INSERT supplies one
via gen_random_uuid(); created_at/updated_at fall to their server_default now().
"""

from collections.abc import Sequence

from alembic import op

revision: str = "25e54e43fcf3"
down_revision: str | None = "5d7bc8c2f5ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "voice_lab:sandbox"
_ROLE_NAME = "VIRTUAL_ASSISTANT"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permission (id, code, description) "
        f"VALUES (gen_random_uuid(), '{_PERMISSION_CODE}', "
        "'Use the Voice Lab sandbox to start and monitor test voice sessions') "
        "ON CONFLICT (code) DO NOTHING"
    )
    op.execute(
        "INSERT INTO role (id, tenant_id, name, description) "
        f"VALUES (gen_random_uuid(), NULL, '{_ROLE_NAME}', '') "
        "ON CONFLICT (tenant_id, name) DO NOTHING"
    )
    # Grant voice_lab:sandbox to VIRTUAL_ASSISTANT itself.
    op.execute(
        "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
        "SELECT gen_random_uuid(), NULL, r.id, p.id "
        "FROM role r, permission p "
        f"WHERE r.tenant_id IS NULL AND r.name = '{_ROLE_NAME}' AND p.code = '{_PERMISSION_CODE}' "
        "ON CONFLICT (role_id, permission_id) DO NOTHING"
    )
    # Backfill: every role (system or tenant-custom) currently holding calls:read
    # also gets voice_lab:sandbox, so existing Voice Lab access survives the switch.
    op.execute(
        "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
        "SELECT gen_random_uuid(), rp.tenant_id, rp.role_id, p_new.id "
        "FROM role_permission rp "
        "JOIN permission p_old ON p_old.id = rp.permission_id AND p_old.code = 'calls:read' "
        f"JOIN permission p_new ON p_new.code = '{_PERMISSION_CODE}' "
        "ON CONFLICT (role_id, permission_id) DO NOTHING"
    )


def downgrade() -> None:
    # Deliberately NOT reversing the data this migration seeded: by the time anyone
    # downgrades, there is no way to distinguish the role_permission rows this
    # migration inserted from rows added since by real product usage (e.g. a tenant
    # admin creating a new custom role via POST /roles with voice_lab:sandbox in its
    # initial permission_ids — that grant is indistinguishable at the row level from
    # this migration's backfill). Blindly deleting by permission code, as an earlier
    # version of this migration did, would silently destroy that live data with no
    # audit trail. If this genuinely needs undoing on an environment with no real
    # usage since (e.g. immediately after a bad deploy), do it by hand after
    # confirming that.
    raise RuntimeError(
        "downgrade unsupported for seed_voice_lab_sandbox_permission_and_role: cannot "
        "safely distinguish this migration's backfilled grants from live product data "
        "added since (see comment above) — revert by hand if truly needed"
    )
