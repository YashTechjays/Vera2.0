"""merge heads

Revision ID: af1d4c138eba
Revises: c247bd741862
Create Date: 2026-08-05 21:07:25.244358

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'af1d4c138eba'
down_revision: str | None = 'c247bd741862'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
