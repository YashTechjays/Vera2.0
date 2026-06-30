"""Event-driven call queue dispatcher.

Pulls admitted forms from the tenant's queue, checks concurrency limits and
insurance-provider working hours, and initiates calls. Invoked on two events:
(1) a form is enqueued, (2) a call ends and a concurrency slot frees up.

No PHI flows through this module — it operates on form IDs, statuses, and
tenant/provider config only.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import Call, CallEvent, InsuranceProvider, PatientForm, Tenant
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
    candidates = (
        (
            await session.execute(
                select(PatientForm)
                .where(
                    PatientForm.tenant_id == tenant_id,
                    PatientForm.status == FormStatus.IN_QUEUE.value,
                )
                .order_by(PatientForm.enqueued_at.asc())
                .limit(slots)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    sm = FormStateMachine()
    dispatched = 0

    for form in candidates:
        # 4a. Expiry check.
        if _is_expired(form, tenant.queue_expiry_hours):
            with contextlib.suppress(InvalidTransitionError):
                sm.transition(form, FormStatus.EXPIRED, tenant_max_retries=tenant.max_retries)
            continue

        # 4b. Working-hours check.
        if not await _provider_in_hours(session, form):
            continue

        # 4c. Dispatch the call.
        call_mode = CallMode.RETRY if form.retry_count > 0 else CallMode.FULL
        sm.transition(form, FormStatus.IN_CALL, tenant_max_retries=tenant.max_retries)

        call = Call(
            tenant_id=tenant_id,
            form_id=form.id,
            current_status=CallStatus.INITIATED.value,
            mode=call_mode.value,
        )
        session.add(call)
        await session.flush()

        room_name = room_name_for_call(tenant_id, call.id)

        tweak = (
            PersonaTweak.model_validate(tenant.persona_tweak)
            if tenant.persona_tweak
            else PersonaTweak()
        )
        metadata = tweak.model_dump(exclude_none=True)
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

    return dispatched


def _is_expired(form: PatientForm, queue_expiry_hours: int) -> bool:
    """True if the form has been in the queue past the expiry window."""
    if form.enqueued_at is None:
        return False
    cutoff = datetime.now(UTC) - timedelta(hours=queue_expiry_hours)
    enqueued = form.enqueued_at
    if enqueued.tzinfo is None:
        enqueued = enqueued.replace(tzinfo=UTC)
    return enqueued < cutoff


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
