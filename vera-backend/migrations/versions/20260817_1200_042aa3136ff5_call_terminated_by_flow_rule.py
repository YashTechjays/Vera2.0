"""call terminated_by_flow_rule

Add terminated_by_flow_rule to call: stamped at closeout when a flow rule cut the
call short (e.g. the plan is inactive), read by the post-call retry decision to
route to human review instead of redialing (VR2-188).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "042aa3136ff5"
down_revision: str | None = "def2df98a870"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: a fresh DB already has the column via 0001's create_all.
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS terminated_by_flow_rule "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS terminated_by_flow_rule")
