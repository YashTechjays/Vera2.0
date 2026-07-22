"""merge coaching-mode speaker_user_id into dev

Revision ID: 919535223bcc
Revises: 4ca8b9e886dc, 5bcbee52fd76
Create Date: 2026-07-22 21:18:07.280753

"""

from collections.abc import Sequence

revision: str = "919535223bcc"
down_revision: str | None = ("4ca8b9e886dc", "5bcbee52fd76")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
