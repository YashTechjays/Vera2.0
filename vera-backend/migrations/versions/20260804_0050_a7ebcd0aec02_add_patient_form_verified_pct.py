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
    op.execute(
        "ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS verified_pct "
        "numeric(5, 2) NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS verified_pct")
