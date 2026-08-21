"""call + its status log and retry lineage.

`current_status` is a maintained enum (CHECK); `call_event` is the append-only
status/phase/health log behind it. Child tables denormalize `tenant_id` for RLS.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import (
    PHI_INFO,
    Base,
    CreatedAtMixin,
    TenantColumnMixin,
    TenantScopedMixin,
    UUIDv7PKMixin,
)
from vera_core.models.enums import CallEventType, CallHealthFlag, CallMode, CallStatus, check_in

# Terminal call statuses — a call in one of these will never become live again.
TERMINAL_CALL_STATUSES = (
    CallStatus.COMPLETED,
    CallStatus.FAILED,
    CallStatus.NO_ANSWER,
    CallStatus.BUSY,
    CallStatus.CANCELED,
)
TERMINAL_CALL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_CALL_STATUSES)
_TERMINAL_SQL = ", ".join(f"'{s.value}'" for s in TERMINAL_CALL_STATUSES)


class Call(Base, TenantScopedMixin):
    __tablename__ = "call"
    __table_args__ = (
        check_in("mode", CallMode),
        check_in("current_status", CallStatus, name="current_status_valid"),
        check_in("health_flag", CallHealthFlag, name="health_flag_valid"),
        Index("ix_call_tenant_status", "tenant_id", "current_status"),
        Index("ix_call_form_id", "form_id"),
        # At most ONE live (non-terminal) call per form — DB backstop against two
        # calls racing onto the same form.
        Index(
            "uq_call_active_form",
            "form_id",
            unique=True,
            postgresql_where=text(f"current_status NOT IN ({_TERMINAL_SQL})"),
        ),
        Index("ix_call_initiated_by", "initiated_by_id", "created_at"),
        Index("ix_call_provider_status", "insurance_provider_id", "current_status"),
        Index("ix_call_tenant_published", "tenant_id", "published"),
        # The analytics history report: tenant's calls over a created_at window.
        Index("ix_call_tenant_created", "tenant_id", "created_at"),
    )

    form_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("patient_form.id", ondelete="RESTRICT"), nullable=False
    )
    insurance_provider_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("insurance_provider.id", ondelete="RESTRICT"),
        nullable=True,
    )
    prompt_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prompt_version.id", ondelete="RESTRICT"), nullable=True
    )
    # The owning supervisor (drives Supervisor-Performance reporting, ADR §6).
    initiated_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    # Durable "a user asked to end this" signal, stamped before the room is torn
    # down: the sweeper closes such a call as CANCELED if call.ended never arrives.
    end_requested_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    # Stamped at closeout (never reset) when a flow rule cut the call short: the post-call
    # retry decision routes such a call to review instead of redialing it (VR2-188).
    terminated_by_flow_rule: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=CallMode.FULL)
    current_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CallStatus.INITIATED
    )
    # Telephony/transport id — operational, not PHI.
    provider_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    call_reference_no: Mapped[str | None] = mapped_column(String(128), nullable=True, info=PHI_INFO)
    rep_info: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, info=PHI_INFO
    )
    completion_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # IVR Success (VR2-45): both stamped once and never rewritten — frozen history,
    # same posture as completion_pct.
    ivr_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ivr_exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Visibility axis, orthogonal to current_status. One-way: once True it never
    # returns to False. False = private to initiated_by_id.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Single-intervener lock: the user currently intervening (NULL = nobody) and
    # the DB-clock claim time driving the stale-lock grace window.
    intervener_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    intervener_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Latest call-health-observer assessment (denormalized at-a-glance state; the
    # transition history lives in call_event HEALTH rows). Deliberately KEPT after
    # the call ends — last-known health feeds reporting. NULL score = never
    # assessed (renders neutrally, never as 0).
    health_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_flag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The analyzer's one-line justification — conversation-derived text (PHI);
    # shown as the health tooltip in Live Monitoring (visibility-gated + audited).
    # Length matches vera_core.call_health.MAX_REASON_LEN (producer caps, the
    # consumer re-truncates on write).
    health_reason: Mapped[str | None] = mapped_column(String(500), nullable=True, info=PHI_INFO)
    health_analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CallLineage(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """retry_call --is-a-retry-of--> parent_call, for tracing retry descent."""

    __tablename__ = "call_lineage"
    __table_args__ = (UniqueConstraint("retry_call_id"),)

    retry_call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False
    )
    parent_call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False, index=True
    )


class CallEvent(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """Append-only status/phase/health/callback log; source for avg holding time /
    IVR-success reports (ADR §6). `event_value` is normalized lowercase."""

    __tablename__ = "call_event"
    __table_args__ = (
        check_in("event_type", CallEventType),
        Index("ix_call_event_call", "call_id", "created_at"),
    )

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    event_value: Mapped[str] = mapped_column(String(64), nullable=False)
    # HEALTH rows store {score, reason, turn_count} here — `reason` is LLM-generated,
    # conversation-derived text and may carry PHI. Relevant to any future
    # column-level protection retrofit (see vera_core/CLAUDE.md — envelope encryption
    # is currently deferred).
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
