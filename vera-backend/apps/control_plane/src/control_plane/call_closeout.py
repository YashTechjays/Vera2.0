"""Shared terminal closeout for a call room: record the call's terminal status,
drive the form edge (with bounded auto-retry), audit the worker-driven form
change. The two writers of worker-driven terminal state — the worker-event
consumer (call.ended / call.failed) and the pipeline sweeper (stuck-call
reconciliation) — both go through here, so the semantics can never diverge.

Idempotent by construction: rooms without a Call row (Voice Lab's synthetic ids)
and already-terminal calls return None untouched, so redeliveries and
consumer/sweeper races are harmless (the row lock serializes them).
"""

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit import AuditRecord, AuditSink
from vera_core.db.rls import tenant_session
from vera_core.models import Call, CallEvent, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.call import TERMINAL_CALL_STATUSES
from vera_core.models.enums import CallEventType, CallStatus
from vera_core.observability.correlation import RoomRef, parse_room_name
from vera_core.services.call_lifecycle import apply_terminal_call_status

logger = logging.getLogger(__name__)

TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_CALL_STATUSES)


async def close_call(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    room_name: str,
    status: CallStatus,
    *,
    trigger: str,
    actor_label: str = "agent-worker",
    end_requested_by: UUID | None = None,
) -> RoomRef | None:
    """Apply *status* as the call's terminal state. Returns the RoomRef when a
    concurrency slot was freed (caller should run a dispatch pass), else None."""
    ref = parse_room_name(room_name)
    if ref is None:
        return None
    async with tenant_session(sessionmaker, ref.tenant_id) as session:
        call = (
            await session.execute(select(Call).where(Call.id == ref.call_id).with_for_update())
        ).scalar_one_or_none()
        if call is None or call.current_status in TERMINAL_VALUES:
            return None  # voice-lab room, or idempotent redelivery / lost race
        if end_requested_by is not None:
            call.end_requested_by_id = end_requested_by
        form = (
            await session.execute(
                select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
            )
        ).scalar_one_or_none()
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == ref.tenant_id))
        ).scalar_one()
        previous_form_status = form.status if form is not None else None
        if form is not None:
            requeued = apply_terminal_call_status(
                call, form, status, tenant_max_retries=tenant.max_retries
            )
            if requeued:
                form.enqueued_at = func.now()
        else:  # form deleted out from under the call — still record the call status
            call.current_status = status.value
        call.ended_at = func.now()
        session.add(
            CallEvent(
                tenant_id=ref.tenant_id,
                call_id=call.id,
                event_type=CallEventType.STATUS.value,
                event_value=status.value,
            )
        )
        if form is not None and form.status != previous_form_status:
            await audit.emit(
                AuditRecord(
                    tenant_id=ref.tenant_id,
                    actor_type=ActorType.SERVICE,
                    actor_user_id=None,
                    actor_label=actor_label,
                    event_type=AuditEvent.FORM_STATUS_CHANGE.value,
                    resource_type="patient_form",
                    resource_id=str(form.id),
                    detail={
                        "from": previous_form_status,
                        "to": form.status,
                        "call_id": str(call.id),
                        "trigger": trigger,
                    },
                )
            )
    return ref
