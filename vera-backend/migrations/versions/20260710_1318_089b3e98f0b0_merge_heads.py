"""Merge the prompt-document data migration with dev's migration line.

`be79c2989c97` (drop legacy pre-PromptDocument prompt_version rows) and dev's
`2cee51fc1076` (itself the platform-mfa / insurance-providers merge) both fork
from `efa94eaaf3f9`; this empty revision joins them so `alembic upgrade head`
sees a single head again (repo rule: resolve multi-head with `just merge-heads`,
never by renumbering).

Revision ID: 089b3e98f0b0
Revises: be79c2989c97, 2cee51fc1076
Create Date: 2026-07-10 13:18:25.622093

"""

from collections.abc import Sequence

revision: str = "089b3e98f0b0"
down_revision: str | Sequence[str] | None = ("be79c2989c97", "2cee51fc1076")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
