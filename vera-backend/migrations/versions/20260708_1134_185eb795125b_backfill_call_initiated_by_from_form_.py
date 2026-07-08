"""backfill call initiated_by from form enqueued_by

Revision ID: 185eb795125b
Revises: d294be3360cd
Create Date: 2026-07-08 11:34:43.204501

Dispatcher-created calls from before ownership threading have
`initiated_by_id IS NULL`, making them invisible in the owner-or-published
list filter. Attribute them to whoever enqueued the form, where known; rows
where the form predates `enqueued_by_id` too stay NULL and are handled at
read time (ownerless calls are tenant-visible).

Data-only backfill; "fill in missing owners" has no meaningful reverse, so
downgrade is a no-op.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "185eb795125b"
down_revision: str | None = "d294be3360cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE call
        SET initiated_by_id = pf.enqueued_by_id
        FROM patient_form pf
        WHERE call.form_id = pf.id
          AND call.initiated_by_id IS NULL
          AND pf.enqueued_by_id IS NOT NULL
        """
    )


def downgrade() -> None:
    pass
