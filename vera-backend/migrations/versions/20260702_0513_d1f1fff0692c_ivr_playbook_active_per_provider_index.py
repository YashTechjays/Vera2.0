"""ivr_playbook: at most one active playbook per provider

Revision ID: d1f1fff0692c
Revises: 0022
Create Date: 2026-07-02 05:13:57.245718

A partial unique index enforces a single ACTIVE ivr_playbook per insurance_provider, so
"the active playbook for this provider" is one indexed lookup and activating a new one
requires demoting the old one first (see api/v1/ivr_playbooks.py). Mirrors
uq_prompt_version_published_per_prompt (migration 0020).

Existing data may already hold more than one active playbook for a provider, which would
block the unique index. The upgrade first de-duplicates: for each provider it keeps the
most-recent active row and demotes the rest to 'inactive', then creates the index. Migration
0001 materializes table DDL from `Base.metadata`, so a fresh DB already has this index from the
model — `CREATE UNIQUE INDEX IF NOT EXISTS` and the no-op de-dup (empty table) make this
migration safe on both fresh and existing DBs.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d1f1fff0692c"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_ivr_playbook_active_per_provider"


def upgrade() -> None:
    # Keep the most-recent active playbook per provider; demote older active duplicates so the
    # partial unique index can be created.
    op.execute(
        """
        UPDATE ivr_playbook SET status = 'inactive'
        WHERE status = 'active'
          AND id NOT IN (
            SELECT DISTINCT ON (provider_id) id
            FROM ivr_playbook
            WHERE status = 'active'
            ORDER BY provider_id, created_at DESC
          )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} "
        "ON ivr_playbook (provider_id) WHERE status = 'active'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
