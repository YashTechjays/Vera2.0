"""prompt_version: at most one published version per prompt

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-29

Mirrors uq_schema_version_published_per_schema for the prompt side. A partial
unique index enforces a single PUBLISHED prompt_version per prompt family, so
"the published prompt for this family" is one indexed lookup and promoting a new
version requires demoting the old one first. Until now this invariant was kept
only in seed code; this makes the DB the guarantor.

Existing data may already hold more than one published version for a prompt,
which would block the unique index. The upgrade first de-duplicates: for each
prompt it keeps the highest-version published row and demotes the rest to DRAFT,
then creates the index. Migration 0001 materializes table DDL from
`Base.metadata`, so a fresh DB already has this index from the model —
`CREATE UNIQUE INDEX IF NOT EXISTS` and the no-op de-dup (empty table) make this
migration safe on both fresh and existing DBs.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_prompt_version_published_per_prompt"


def upgrade() -> None:
    # Keep the highest-version published row per prompt; demote older published
    # duplicates to DRAFT so the partial unique index can be created.
    op.execute(
        """
        UPDATE prompt_version SET status = 'draft'
        WHERE status = 'published'
          AND id NOT IN (
            SELECT DISTINCT ON (prompt_id) id
            FROM prompt_version
            WHERE status = 'published'
            ORDER BY prompt_id, version DESC
          )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} "
        "ON prompt_version (prompt_id) WHERE status = 'published'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
