"""Event-driven call queue dispatcher.

Pulls admitted forms from the tenant's queue, checks concurrency limits and
insurance-provider working hours, and initiates calls. Invoked on two events:
(1) a form is enqueued, (2) a call ends and a concurrency slot frees up.

No PHI flows through this module — it operates on form IDs, statuses, and
tenant/provider config only.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.audit import AuditRecord
from vera_core.call_plan import CallPlanStore, build_and_store_call_plan
from vera_core.models import Call, CallEvent, InsuranceProvider, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import (
    CallEventType,
    CallMode,
    CallStatus,
    FormStatus,
)
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import PersonaTweak
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError

if TYPE_CHECKING:
    from uuid import UUID

    from vera_core.audit import AuditSink

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

# Form statuses that count toward the tenant's concurrency cap.
_ACTIVE_FORM_STATUSES = (
    FormStatus.IN_CALL.value,
    FormStatus.AI_PROCESSING.value,
)


def _now_eastern_time() -> time:
    """Current wall-clock time in US Eastern. Extracted for test patching."""
    return datetime.now(_EASTERN).time()


def is_within_working_hours(provider: InsuranceProvider) -> bool:
    """Return True if *provider* is within its configured working-hours window.

    When either bound is ``None`` the gate is fully open — hours are opt-in.
    """
    if provider.working_hour_start is None or provider.working_hour_end is None:
        return True
    now_time = _now_eastern_time()
    return provider.working_hour_start <= now_time <= provider.working_hour_end


async def try_dispatch(
    session: AsyncSession,
    tenant_id: UUID,
    livekit: Any,
    *,
    audit: AuditSink | None = None,
    call_plan_store: CallPlanStore | None = None,
) -> int:
    """Attempt to dispatch queued forms for *tenant_id*.

    Returns the number of calls initiated. Designed to be called after commit
    of the triggering event (enqueue or call-end).

    Parameters
    ----------
    session:
        An active ``AsyncSession`` scoped to *tenant_id* (RLS active).
    tenant_id:
        The tenant whose queue to drain.
    livekit:
        A ``LiveKitGateway`` (or duck-typed fake) with ``create_call_room``.
    audit:
        Optional ``AuditSink``. When provided, ``QUEUE_DISPATCH`` and
        ``QUEUE_EXPIRED`` events are emitted for HIPAA evidence.
    call_plan_store:
        Optional ``CallPlanStore``. When provided, each dispatched call's schema is
        compiled into a plan and stashed for the worker (v1 forms write nothing).
    """
    # 1. Load tenant config.
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        logger.warning("dispatch: tenant %s not found", tenant_id)
        return 0

    # 2. Count active calls (forms currently IN_CALL or AI_PROCESSING).
    active_count: int = (
        await session.execute(
            select(func.count())
            .select_from(PatientForm)
            .where(
                PatientForm.tenant_id == tenant_id,
                PatientForm.status.in_(list(_ACTIVE_FORM_STATUSES)),
            )
        )
    ).scalar_one()

    slots = tenant.max_agents_per_va - active_count
    if slots <= 0:
        return 0

    # 3. Fetch FIFO candidates — FOR UPDATE SKIP LOCKED prevents double-dispatch.
    # The expiry filter is pushed into the DB WHERE clause to use the DB clock,
    # avoiding Python/DB clock skew (HIPAA timestamp-of-record requirement).
    # func.make_interval takes positional args (years, months, weeks, days, hours, ...);
    # SQLAlchemy's func does not forward keyword args, so hours is the 5th positional.
    expiry_interval = func.make_interval(0, 0, 0, 0, tenant.queue_expiry_hours)
    candidates = (
        (
            await session.execute(
                select(PatientForm)
                .where(
                    PatientForm.tenant_id == tenant_id,
                    PatientForm.status == FormStatus.IN_QUEUE.value,
                    (PatientForm.enqueued_at.is_(None))
                    | (PatientForm.enqueued_at > func.now() - expiry_interval),
                )
                .order_by(PatientForm.enqueued_at.asc())
                .limit(slots)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    # Expired forms (outside the window) are handled in a separate pass so they
    # get the EXPIRED transition and audit without blocking live candidates.
    expired_candidates = (
        (
            await session.execute(
                select(PatientForm)
                .where(
                    PatientForm.tenant_id == tenant_id,
                    PatientForm.status == FormStatus.IN_QUEUE.value,
                    PatientForm.enqueued_at.is_not(None),
                    PatientForm.enqueued_at <= func.now() - expiry_interval,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    sm = FormStateMachine()

    # 4a. Expire stale forms.
    for form in expired_candidates:
        try:
            sm.transition(form, FormStatus.EXPIRED, tenant_max_retries=tenant.max_retries)
            logger.info("dispatch: expired form %s", form.id)
            if audit is not None:
                await audit.emit(
                    AuditRecord(
                        tenant_id=tenant_id,
                        actor_type=ActorType.SYSTEM,
                        actor_label="queue-dispatcher",
                        event_type=AuditEvent.QUEUE_EXPIRED.value,
                        resource_type="patient_form",
                        resource_id=str(form.id),
                    )
                )
        except InvalidTransitionError:
            pass

    dispatched = 0

    # Room metadata is tenant-level — compute it once for the whole pass.
    tweak = (
        PersonaTweak.model_validate(tenant.persona_tweak)
        if tenant.persona_tweak
        else PersonaTweak()
    )
    metadata = tweak.model_dump(exclude_none=True)

    for form in candidates:
        # 4b. Working-hours check.
        if not await _provider_in_hours(session, form):
            continue

        # 4c. Dispatch the call — wrap in try/except so one failure does not
        # roll back successfully-dispatched calls earlier in the same pass.
        call_mode = CallMode.RETRY if form.retry_count > 0 else CallMode.FULL
        try:
            sm.transition(form, FormStatus.IN_CALL, tenant_max_retries=tenant.max_retries)

            # Insert the Call and provision its room inside a savepoint: if
            # create_call_room fails, the savepoint rolls back the flushed Call
            # so a failed dispatch leaves no orphan INITIATED row behind (the
            # form is reverted to IN_QUEUE in the except handler below).
            async with session.begin_nested():
                call = Call(
                    tenant_id=tenant_id,
                    form_id=form.id,
                    current_status=CallStatus.INITIATED.value,
                    mode=call_mode.value,
                    # Whoever enqueued the form owns the call, however late dispatch runs.
                    initiated_by_id=form.enqueued_by_id,
                )
                session.add(call)
                await session.flush()

                room_name = room_name_for_call(tenant_id, call.id)
                if call_plan_store is not None:
                    await build_and_store_call_plan(
                        session,
                        form=form,
                        call_id=call.id,
                        room_name=room_name,
                        store=call_plan_store,
                    )
                await livekit.create_call_room(room_name, metadata=metadata)

                session.add(
                    CallEvent(
                        tenant_id=tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.INITIATED.value,
                    )
                )
            dispatched += 1
            logger.info(
                "dispatch: initiated call %s for form %s (mode=%s)",
                call.id,
                form.id,
                call_mode.value,
            )
            if audit is not None:
                await audit.emit(
                    AuditRecord(
                        tenant_id=tenant_id,
                        actor_type=ActorType.SYSTEM,
                        actor_label="queue-dispatcher",
                        event_type=AuditEvent.QUEUE_DISPATCH.value,
                        resource_type="patient_form",
                        resource_id=str(form.id),
                        detail={"call_id": str(call.id), "mode": call_mode.value},
                    )
                )
        except Exception:
            logger.exception(
                "dispatch: failed to dispatch form %s — reverting to IN_QUEUE", form.id
            )
            # The savepoint rolled back the Call; revert the in-memory form to
            # IN_QUEUE so it will be retried on the next dispatch pass.
            form.status = FormStatus.IN_QUEUE.value

    return dispatched


async def _provider_in_hours(session: AsyncSession, form: PatientForm) -> bool:
    """Resolve the form's insurance provider and check working hours.

    If the form has no linked provider name, or the name resolves to no active
    provider record, the gate is open — working-hours enforcement is opt-in.
    """
    if not form.insurance_provider:
        return True
    provider = (
        await session.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.name == form.insurance_provider,
                InsuranceProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
    if provider is None:
        return True
    return is_within_working_hours(provider)
