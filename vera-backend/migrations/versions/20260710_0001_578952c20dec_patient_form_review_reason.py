"""patient_form review_reason

Add review_reason column to patient_form: stamped by the post-call pipeline when
routing to EXCEPTION_REVIEW, cleared on every other outcome and on manual exit.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "578952c20dec"
down_revision: str | None = "3f7a9c2e8b41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS review_reason VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS review_reason")
