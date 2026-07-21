"""Oversight, disclosure, evals and provider telemetry.

`intervention_event.category` is a first-class CHECK column so the
intervention-by-category report is a GROUP BY, not a JSONB scan (ADR §6).
`call_provider_usage` is the home the spec §4.7.2 cost/latency reports lacked.
`eval_run` is authoring-catalog scoped (no tenant_id, no RLS) — it evaluates a
global `prompt_version`. Export-disclosure ledger lives in `export_artifact.py`.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import (
    PHI_INFO,
    Base,
    CreatedAtMixin,
    TenantColumnMixin,
    UUIDv7PKMixin,
)
from vera_core.models.enums import (
    EvalScope,
    InterventionCategory,
    InterventionType,
    ProviderStage,
    check_in,
)


class InterventionEvent(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """Coaching/whisper/takeover audit trail. An intervention "occurred" = a row
    exists; `category` drives the by-category chart; `supervisor_id` drives
    interventions-per-supervisor (ADR §6)."""

    __tablename__ = "intervention_event"
    __table_args__ = (
        check_in("type", InterventionType),
        check_in("category", InterventionCategory),
        Index("ix_intervention_call_category", "call_id", "category"),
    )

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False
    )
    supervisor_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, info=PHI_INFO
    )


class HumanRating(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """Human rater feedback on a call (evals harness, spec §4.7.1)."""

    __tablename__ = "human_rating"
    __table_args__ = (CheckConstraint("score BETWEEN 1 AND 5", name="score_range"),)

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("call.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Free-text rater note — may quote the call, so treated as PHI.
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="", info=PHI_INFO)


class CallProviderUsage(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """One row per (call, stage, provider) — cost/latency telemetry (ADR §6).
    Operational metrics, not PHI."""

    __tablename__ = "call_provider_usage"
    __table_args__ = (
        check_in("stage", ProviderStage),
        Index("ix_provider_usage_call_stage", "call_id", "stage"),
    )

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(8), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EvalRun(Base, UUIDv7PKMixin, CreatedAtMixin):
    """An evals run against a prompt version — authoring-catalog scoped (no
    tenant_id / no RLS), like the prompt it evaluates."""

    __tablename__ = "eval_run"
    __table_args__ = (check_in("scope", EvalScope),)

    prompt_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("prompt_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
