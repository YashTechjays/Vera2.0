"""drop legacy pre-PromptDocument prompt_version rows

Data migration. `prompt_version.composite_json` changed meaning with the
prompt-compiler work (2026-07-08 spec §4): it now holds a `PromptDocument`
(`{"kind": "prompt_document", "session": …, "task_overrides": …}`) instead of the
old seed-time compiled composite. Rows in the old shape are unreadable by the
current application — `PromptDocument.model_validate` raises on them, crashing the
seeder's carry-forward and the prompts preview endpoint — and every one of them was
machine-generated seed output (the operator editor never shipped against the old
shape), so nothing hand-authored is lost.

Deletes only the legacy VERSION rows (never whole prompt families), so any
new-shape drafts survive; the next `seed.py` run bootstraps a factory
PromptDocument as the published version. Idempotent; a no-op on fresh databases.

FK edges this DELETE rests on (verified: nothing writes either column yet):
`eval_run.prompt_version_id` is ON DELETE CASCADE — eval runs recorded against a
legacy version would be deleted silently along with it; `call.prompt_version_id`
is RESTRICT — a call pinned to a legacy version would abort the migration. Both
are safe today only because no code path populates those columns; if that changes
before this migration has run everywhere, revisit.

The DELETE statement lives in `LEGACY_DELETE_SQL` so the integration test
(`tests/integration/db/test_prompt_version_data_migration.py`) exercises the
exact statement this migration runs.

Revision ID: be79c2989c97
Revises: efa94eaaf3f9
Create Date: 2026-07-09 13:21:27.413114

"""

from collections.abc import Sequence

from alembic import op

revision: str = "be79c2989c97"
down_revision: str | None = "efa94eaaf3f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `-> 'kind' IS NULL` rather than the `?` operator: composite_json is JSON (not
# JSONB, key order matters), and `->` casts cleanly without driver paramstyle
# quirks around `?`.
LEGACY_DELETE_SQL = "DELETE FROM prompt_version WHERE (composite_json::jsonb -> 'kind') IS NULL"


def upgrade() -> None:
    op.execute(LEGACY_DELETE_SQL)


def downgrade() -> None:
    # Irreversible data deletion: the removed rows were regenerable seed artifacts
    # in a shape the application can no longer produce or read. Nothing to restore.
    pass
