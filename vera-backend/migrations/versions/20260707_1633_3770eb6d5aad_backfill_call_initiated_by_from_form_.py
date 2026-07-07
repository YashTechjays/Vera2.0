"""backfill call initiated_by from form enqueued_by

Revision ID: 3770eb6d5aad
Revises: d42dc1a464b8
Create Date: 2026-07-07 16:33:09.449157

Dispatcher-created calls from before ownership threading have
`initiated_by_id IS NULL`, making them invisible in the owner-or-published
list filter. Attribute them to whoever enqueued the form, where that is
known. Rows where the form predates `enqueued_by_id` too stay NULL and are
handled at read time (ownerless calls are tenant-visible).

Data-only backfill; the reverse of "fill in missing owners" is unknowable,
so downgrade is a no-op.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3770eb6d5aad"
down_revision: str | None = "d42dc1a464b8"
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
