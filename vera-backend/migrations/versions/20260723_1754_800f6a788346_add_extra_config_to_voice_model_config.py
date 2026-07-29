"""add extra_config to voice_model_config

Revision ID: 800f6a788346
Revises: e3e633747040
Create Date: 2026-07-23 17:54:14.468649

Additive, nullable JSONB column — NULL means "no thinking override, use the
per-family default" (see agent_worker/cascade.py::resolve_thinking_attrs). No
CHECK constraint: the single write path (vera_core.services.model_config.save_llm_model)
validates its shape via ThinkingOverride before insert.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "800f6a788346"
down_revision: str | None = "e3e633747040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE voice_model_config ADD COLUMN IF NOT EXISTS extra_config JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE voice_model_config DROP COLUMN IF EXISTS extra_config")
