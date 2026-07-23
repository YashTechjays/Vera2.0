"""Global, append-only log of voice-cascade model overrides (GLOBAL: no tenant_id, no
RLS, no PHI) — a platform surface a SUPER_ADMIN curates, mirroring Prompt/PromptVersion.

Never updated in place: every save or reset is a new row (CreatedAtMixin, no `is_active`
flag). The newest row per `stage` IS the current effective value — `model IS NULL`
(paired with `provider IS NULL`) is the explicit "reset to default" state, a real
queryable row rather than an absence of rows. Only `stage == VoiceModelStage.LLM` is
written today; STT/TTS are schema-ready for a future iteration (see agent_worker/cascade.py).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, CreatedAtMixin, UUIDv7PKMixin
from vera_core.models.enums import VoiceModelStage, check_in


class VoiceModelConfig(Base, UUIDv7PKMixin, CreatedAtMixin):
    __tablename__ = "voice_model_config"

    __table_args__ = (
        check_in("stage", VoiceModelStage),
        CheckConstraint(
            "(model IS NULL AND provider IS NULL) OR (model IS NOT NULL AND provider IS NOT NULL)",
            name="model_provider_pair",
        ),
        Index("ix_voice_model_config_stage_created_at", "stage", "created_at"),
    )

    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extra_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id"), nullable=True
    )
