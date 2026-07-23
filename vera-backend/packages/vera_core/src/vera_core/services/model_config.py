"""Voice-cascade model override — currently LLM (Google) only; the table generalizes to
STT/TTS for a future iteration. Global, append-only: `get_active_llm_config` and
`list_llm_config_history` only read `voice_model_config`; `save_llm_model` and
`reset_llm_model` only ever INSERT, mirroring VoiceModelConfig's append-only shape.
"""

import logging
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import VoiceModelConfig
from vera_core.models.enums import VoiceModelStage

logger = logging.getLogger(__name__)

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_MODEL_NAME_LENGTH = 200
_LLM_PROVIDER = "google"


class InvalidModelName(ValueError):
    pass


def normalize_model_name(raw: str) -> str:
    """Trim + validate a freeform model name. Deliberately permissive charset (letters,
    digits, dot, hyphen, underscore) — covers every real Gemini model id — while still
    rejecting empty/whitespace-only input and anything absurdly long. No live Vertex AI
    check (control_plane has no aiplatform IAM grant yet — adr/devops-todo.md)."""
    trimmed = raw.strip()
    if not trimmed:
        raise InvalidModelName("model name must not be empty")
    if len(trimmed) > _MAX_MODEL_NAME_LENGTH:
        raise InvalidModelName(f"model name must be at most {_MAX_MODEL_NAME_LENGTH} characters")
    if not _MODEL_NAME_RE.match(trimmed):
        raise InvalidModelName("model name may only contain letters, digits, '.', '-', '_'")
    return trimmed


async def get_active_llm_config(session: AsyncSession) -> VoiceModelConfig | None:
    """The newest voice_model_config row for the llm stage with an active override, or None
    if never written OR if the newest row is an explicit reset (model IS NULL). A None return
    always means "use the hardcoded default"—never returns a row with .model is None."""
    config = (
        await session.execute(
            select(VoiceModelConfig)
            .where(VoiceModelConfig.stage == VoiceModelStage.LLM)
            .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # If the newest row has model=None, treat it the same as no row existing (use default)
    if config is not None and config.model is None:
        return None

    return config


async def list_llm_config_history(
    session: AsyncSession, *, limit: int = 50
) -> Sequence[VoiceModelConfig]:
    return (
        (
            await session.execute(
                select(VoiceModelConfig)
                .where(VoiceModelConfig.stage == VoiceModelStage.LLM)
                .order_by(VoiceModelConfig.created_at.desc(), VoiceModelConfig.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def save_llm_model(
    session: AsyncSession, raw_model: str, *, created_by_user_id: UUID | None
) -> VoiceModelConfig:
    row = VoiceModelConfig(
        stage=VoiceModelStage.LLM,
        provider=_LLM_PROVIDER,
        model=normalize_model_name(raw_model),
        created_by_user_id=created_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row


async def reset_llm_model(
    session: AsyncSession, *, created_by_user_id: UUID | None
) -> VoiceModelConfig | None:
    """No-op (returns None) if already at default; otherwise inserts an explicit reset
    row (provider/model both NULL) so history shows who cleared it and when."""
    current = await get_active_llm_config(session)
    if current is None:
        return None
    row = VoiceModelConfig(
        stage=VoiceModelStage.LLM,
        provider=None,
        model=None,
        created_by_user_id=created_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row


async def add_llm_model_override_metadata(session: AsyncSession, metadata: dict[str, Any]) -> None:
    """Mirrors add_active_playbook_metadata's missing-key convention: a missing
    `llm_model_override` key means "use the hardcoded cascade default". A broken config
    table must never block a call from being placed, so any read failure degrades to the
    same missing-key default rather than propagating and failing the whole dispatch."""
    try:
        current = await get_active_llm_config(session)
    except Exception as exc:
        logger.warning("llm model override lookup failed (%s) — using default", type(exc).__name__)
        return
    if current is not None:
        metadata["llm_model_override"] = current.model
