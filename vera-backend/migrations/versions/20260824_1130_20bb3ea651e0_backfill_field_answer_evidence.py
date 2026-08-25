"""backfill field_answer evidence from its transcript anchor

Revision ID: 20bb3ea651e0
Revises: 042aa3136ff5
Create Date: 2026-08-24 11:30:00.000000

Every answer the live Observer wrote landed with only an `evidence_seq` pointer —
the resolved text was never stored — so a reviewer opening an already-completed
form sees evidence on just the fields the post-call judge happened to quote.
`post_call_eval` now resolves the anchor for new calls; this fills in the forms
that already closed, resolving each pointer against the persisted transcript.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20bb3ea651e0"
down_revision: str | None = "042aa3136ff5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_STATEMENTS: tuple[str, ...] = (
    # `field_answer.evidence_seq` and `transcript.seq` are the same numbering by
    # construction (tests/unit/test_evidence_seq_parity.py), so the join IS the
    # resolution. Idempotent: a row that already carries evidence no longer matches
    # `IS NULL`, and an anchor with no surviving transcript row simply stays NULL.
    """
    UPDATE field_answer fa SET evidence = t.message
    FROM transcript t
    WHERE t.call_id = fa.call_id
      AND t.seq = fa.evidence_seq
      AND fa.evidence IS NULL
      AND fa.evidence_seq IS NOT NULL
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Data backfill — nothing to reverse (the previous state was "never written").
    pass
