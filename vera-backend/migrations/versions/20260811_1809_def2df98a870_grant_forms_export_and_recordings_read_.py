"""grant forms export and recordings read to virtual assistant

Revision ID: def2df98a870
Revises: d8cee818167e
Create Date: 2026-08-11 18:09:28.595813

The VIRTUAL_ASSISTANT global system role gains access to export a completed
form (forms:export) and play back call recordings (recordings:read),
mirroring rbac_defaults.py. Both permission codes already exist (seeded by
76a992faa2c5 and 7a471dd8ce48) — this only grants them to VIRTUAL_ASSISTANT.

Runs on the privileged migration connection (not RLS-bound) — the strict
WITH CHECK on NULL-tenant role_permission rows does not block it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "def2df98a870"
down_revision: str | None = "d8cee818167e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODES = ("forms:export", "recordings:read")


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
        "downgrade unsupported for grant_forms_export_and_recordings_read_to_virtual_assistant: "
        "cannot safely distinguish this migration's grants from live product data added since"
    )
