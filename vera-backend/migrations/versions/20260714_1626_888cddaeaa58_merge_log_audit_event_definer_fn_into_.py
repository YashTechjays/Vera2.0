"""merge log_audit_event definer fn into dev head

Revision ID: 888cddaeaa58
Revises: 7be43a8bdf5c, 9514979e3fee
Create Date: 2026-07-14 16:26:50.885701

Empty join revision: `9514979e3fee` (log_audit_event definer fn) was cut from
`65ab40d4f511` before `7be43a8bdf5c` merged that revision's sibling head into
dev, so both forked from it independently. This unifies them so `alembic
upgrade head` resolves to a single head.
"""

from collections.abc import Sequence

revision: str = "888cddaeaa58"
down_revision: str | None = ("7be43a8bdf5c", "9514979e3fee")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
