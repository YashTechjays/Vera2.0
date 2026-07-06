"""call + its status log and retry lineage.

`current_status` is a maintained enum (CHECK), not a window-scan over free-text —
the v1 P2 fix; `call_event` is the append-only status/phase/health log behind it.
`prompt_version_id` pins exactly which prompt version ran the call (lineage). All
child tables denormalize `tenant_id` for plain-column RLS.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, text
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
from vera_core.models.enums import CallEventType, CallMode, CallStatus, check_in

# Terminal call statuses — a call in one of these will never become live again.
# Keep in sync with the callback's accepted statuses (api/v1/calls.py).
TERMINAL_CALL_STATUSES = (
    CallStatus.COMPLETED,
    CallStatus.FAILED,
    CallStatus.NO_ANSWER,
    CallStatus.BUSY,
)
_TERMINAL_SQL = ", ".join(f"'{s.value}'" for s in TERMINAL_CALL_STATUSES)


class Call(Base, TenantScopedMixin):
    __tablename__ = "call"
    __table_args__ = (
        check_in("mode", CallMode),
        check_in("current_status", CallStatus, name="current_status_valid"),
        Index("ix_call_tenant_status", "tenant_id", "current_status"),
        Index("ix_call_form_id", "form_id"),
        # At most ONE live (non-terminal) call per form — DB backstop against a
        # manual call and a dispatcher call racing onto the same form.
        Index(
            "uq_call_active_form",
            "form_id",
            unique=True,
            postgresql_where=text(f"current_status NOT IN ({_TERMINAL_SQL})"),
        ),
        Index("ix_call_initiated_by", "initiated_by_id", "created_at"),
        Index("ix_call_provider_status", "insurance_provider_id", "current_status"),
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

    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=CallMode.FULL)
    current_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CallStatus.INITIATED
    )
    # Telephony/transport id (Twilio/LiveKit) — operational, not PHI.
    provider_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    call_reference_no: Mapped[str | None] = mapped_column(String(128), nullable=True, info=PHI_INFO)
    rep_info: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, info=PHI_INFO
    )
    completion_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CallLineage(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """retry_call --is-a-retry-of--> parent_call. Complements
    `field_answer.is_current` (the merge mechanism) for tracing retry descent."""

    __tablename__ = "call_lineage"
    __table_args__ = (UniqueConstraint("retry_call_id"),)

    retry_call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False
    )
    parent_call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False, index=True
    )


class CallEvent(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """Append-only status/phase/health/callback log; the source for avg holding
    time / IVR-success reports (derived from phase transitions, ADR §6).
    `event_value` is normalized lowercase — closes the v1 casing problem."""

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
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
