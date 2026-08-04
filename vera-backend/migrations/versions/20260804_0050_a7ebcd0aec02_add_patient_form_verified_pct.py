"""add patient_form.verified_pct

Revision ID: a7ebcd0aec02
Revises: c42e477a6e8d
Create Date: 2026-08-04 00:50:54.478916

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7ebcd0aec02"
down_revision: str | None = "c42e477a6e8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: existing rows (and any form not yet post-call-evaluated)
    # read NULL = "not evaluated", never a misleading 0. Diverges from completion_pct
    # deliberately — see the model comment.
    op.execute("ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS verified_pct numeric(5, 2)")
    # A DB provisioned from this migration's earlier (NOT NULL DEFAULT 0) revision already
    # has the column — ADD COLUMN no-ops there — so bring it to the nullable, no-default
    # shape explicitly. Both ALTERs are harmless no-ops where the column is already nullable
    # (fresh CI: 0001 create_all builds it from the current model).
    op.execute("ALTER TABLE patient_form ALTER COLUMN verified_pct DROP NOT NULL")
    op.execute("ALTER TABLE patient_form ALTER COLUMN verified_pct DROP DEFAULT")


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS verified_pct")
