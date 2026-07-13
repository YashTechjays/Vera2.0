"""drop dead patient_form scheduled_at

`scheduled_at` was the v1 queue-ordering column; migration `1e6c84132026`
(call queue columns) replaced it with `enqueued_at` for FIFO ordering and
nothing has read or written it since — no code path, index, DTO, or UI
references it. Dropped as dead weight (all rows NULL).

Idempotent: migration `0001`'s `create_all` builds fresh DBs off the live
model, which no longer has the column — `IF EXISTS` keeps the fresh-DB CI
run a no-op. Downgrade restores the (nullable, empty) column; the dropped
values are not recoverable, but none existed.

Revision ID: 3083477bf7a5
Revises: 1b33c1a69f36
Create Date: 2026-07-11 21:44:06.830884

"""

from collections.abc import Sequence

from alembic import op

revision: str = "3083477bf7a5"
down_revision: str | None = "1b33c1a69f36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS scheduled_at")


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ")
