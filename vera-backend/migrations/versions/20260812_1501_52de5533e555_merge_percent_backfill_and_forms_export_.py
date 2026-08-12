"""merge percent backfill and forms-export grant heads

Revision ID: 52de5533e555
Revises: def2df98a870, 5752b98fa277
Create Date: 2026-08-12 15:01:58.632532

Two branches off `d8cee818167e`: dev's `def2df98a870` (grant forms-export and
recordings-read permissions) and this branch's `5752b98fa277` (backfill percent
field answers). Both are independent — an RBAC permission grant versus a
`field_answer` value rewrite — so their relative order does not matter and this
merge revision carries no work of its own.

"""

from collections.abc import Sequence

revision: str = "52de5533e555"
down_revision: str | None = ("def2df98a870", "5752b98fa277")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
