"""patient_form enqueued_by for call ownership

`enqueued_by_id` persists the user who queued a form so the dispatcher can
attribute `call.initiated_by_id` to them even when the call is actually
created later by a different actor (freed-slot dispatch, retry-at-callback).

Revision ID: 62947d8a3be7
Revises: 0eacf358cad7
Create Date: 2026-07-06 15:34:23.375912

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "62947d8a3be7"
down_revision: str | None = "0eacf358cad7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("patient_form", sa.Column("enqueued_by_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_patient_form_enqueued_by_id_app_user"),
        "patient_form",
        "app_user",
        ["enqueued_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_patient_form_enqueued_by_id_app_user"), "patient_form", type_="foreignkey"
    )
    op.drop_column("patient_form", "enqueued_by_id")
