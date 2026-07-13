"""Event-driven call queue dispatcher.

Pulls admitted forms from the tenant's queue, checks concurrency limits and
insurance-provider working hours, and initiates calls. Invoked on two events:
(1) a form is enqueued, (2) a call ends and a concurrency slot frees up.

No PHI flows through this module — it operates on form IDs, statuses, and
tenant/provider config only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.audit import AuditRecord
from vera_core.integrations.credentials import get_integration_credentials
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
from vera_core.services.call_lifecycle import apply_terminal_call_status
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.ivr_selection import (
    add_active_playbook_metadata,
    add_ivr_call_data_metadata,
)
from vera_core.telephony import OutboundDialError

if TYPE_CHECKING:
    from uuid import UUID

    from vera_core.audit import AuditSink
    from vera_core.config.kms import KeyManagementService

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

# Form statuses that count toward the tenant's concurrency cap.
_ACTIVE_FORM_STATUSES = (
    FormStatus.IN_CALL.value,
    FormStatus.AI_PROCESSING.value,
)

_DISPATCH_LOCK_CLASS = 0x76455241  # "vERA" — namespace for dispatch advisory locks


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
    kms: KeyManagementService | Any,
    *,
    audit: AuditSink | None = None,
    dial_pacing_s: float = 1.0,
) -> int:
    """Attempt to dispatch queued forms for *tenant_id*.

    Returns the number of calls initiated (dial-failed calls do NOT count,
    even though a Call row was created for them). Designed to be called after
    commit of the triggering event (enqueue or call-end).

    Parameters
    ----------
    session:
        An active ``AsyncSession`` scoped to *tenant_id* (RLS active).
    tenant_id:
        The tenant whose queue to drain.
    livekit:
        A ``LiveKitGateway`` (or duck-typed fake) with ``create_call_room``,
        ``create_sip_participant``, and ``delete_room``.
    kms:
        The ``KeyManagementService`` used to open the tenant's sealed outbound
        SIP trunk credential.
    audit:
        Optional ``AuditSink``. When provided, ``QUEUE_DISPATCH`` and
        ``QUEUE_EXPIRED`` events are emitted for HIPAA evidence.
    dial_pacing_s:
        Seconds to sleep between successive dials in one pass, to stay under
        the carrier's calls-per-second limit (Twilio ~1 CPS). Applied between
        dials only — never before the first.
    """
    # Serialize dispatch passes per tenant (consumer refill / sweeper / enqueue
    # tasks race otherwise and can over-allocate concurrency slots): the two-int
    # advisory lock is transaction-scoped, so it releases on commit/rollback.
    # close_call takes row locks without this lock — no ordering inversion, since
    # the advisory lock is always acquired first, at the very start of a pass.
    await session.execute(
        select(func.pg_advisory_xact_lock(_DISPATCH_LOCK_CLASS, func.hashtext(str(tenant_id))))
    )

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
    # The working-hours gate is ALSO pushed into the WHERE: filtered after a
    # LIMIT(slots) FIFO fetch, a few stale forms for a closed provider would
    # occupy the whole window and starve every dispatchable form behind them
    # for up to the expiry horizon (head-of-line blocking). Mirrors
    # _resolve_provider + is_within_working_hours: only an ACTIVE provider with
    # both bounds set can be outside its window.
    now_eastern = _now_eastern_time()
    provider_outside_hours = (
        select(InsuranceProvider.id)
        .where(
            InsuranceProvider.name == PatientForm.insurance_provider,
            InsuranceProvider.status == "active",
            InsuranceProvider.working_hour_start.is_not(None),
            InsuranceProvider.working_hour_end.is_not(None),
            or_(
                InsuranceProvider.working_hour_start > now_eastern,
                InsuranceProvider.working_hour_end < now_eastern,
            ),
        )
        .exists()
    )
    candidates = (
        (
            await session.execute(
                select(PatientForm)
                .where(
                    PatientForm.tenant_id == tenant_id,
                    PatientForm.status == FormStatus.IN_QUEUE.value,
                    (PatientForm.enqueued_at.is_(None))
                    | (PatientForm.enqueued_at > func.now() - expiry_interval),
                    ~provider_outside_hours,
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
    dial_attempted = False

    # Tenant-level persona overlay — computed once, nested per-call below.
    tweak = (
        PersonaTweak.model_validate(tenant.persona_tweak)
        if tenant.persona_tweak
        else PersonaTweak()
    )
    tweak_fields = tweak.model_dump(exclude_none=True)

    # Resolve the tenant's outbound SIP trunk once for the whole pass — every
    # candidate dials through the same trunk. Skip the lookup entirely when
    # there's nothing to dispatch.
    trunk_id: str | None = None
    if candidates:
        creds = await get_integration_credentials(
            session, kms, integration_type_name="livekit_outbound_trunk_id"
        )
        trunk_id = creds.get("trunk_id") if creds else None
        if not trunk_id:
            # The enqueue gate normally prevents this; config may have changed since.
            logger.warning(
                "dispatch: tenant %s has queued forms but no outbound trunk; leaving queued",
                tenant_id,
            )
            candidates = []

    for form in candidates:
        # 4b. Working-hours re-check at dial time — the SQL gate above used the
        # pass-start clock, and the window can close mid-pass (dial pacing).
        # Provider reused below for id + playbook.
        provider = await _resolve_provider(session, form)
        if provider is not None and not is_within_working_hours(provider):
            continue

        call_mode = CallMode.RETRY if form.retry_count > 0 else CallMode.FULL
        # Real-call dispatch metadata: the worker must wait for the SIP callee to
        # answer and publish envelope events for live monitoring. IVR navigation is
        # the operator's per-form queue-time choice (voice-lab-style toggle) — when
        # ON, the provider's active playbook (if any) specializes the navigator.
        metadata: dict[str, Any] = {
            "wait_for_speaker": True,
            "publish_events": True,
        }
        if form.ivr_navigation_enabled:
            metadata["enable_ivr_navigation"] = True
        if tweak_fields:
            metadata["persona_tweak"] = tweak_fields

        # 4c. Create the call + room — wrap in try/except so one failure does not
        # roll back successfully-dispatched calls earlier in the same pass.
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
                    insurance_provider_id=provider.id if provider else None,
                )
                session.add(call)
                await session.flush()
                room_name = room_name_for_call(tenant_id, call.id)
                if form.ivr_navigation_enabled and provider is not None:
                    await add_active_playbook_metadata(session, provider.id, metadata)
                # Patient/provider identifiers for the navigator: form-only (no provider
                # gate, unlike the playbook), read off the already-loaded form (no queries).
                if form.ivr_navigation_enabled:
                    add_ivr_call_data_metadata(form, metadata)
                await livekit.create_call_room(room_name, metadata=metadata)
                session.add(
                    CallEvent(
                        tenant_id=tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.INITIATED.value,
                    )
                )
        except Exception:
            logger.exception(
                "dispatch: failed to dispatch form %s — reverting to IN_QUEUE", form.id
            )
            # The savepoint rolled back the Call; revert the in-memory form to
            # IN_QUEUE so it will be retried on the next dispatch pass.
            form.status = FormStatus.IN_QUEUE.value
            continue

        # 4d. Dial OUTSIDE the savepoint: a failed dial keeps the Call row as
        # evidence (FAILED + retry accounting) instead of rolling it back. Pace
        # every dial attempt ~1/s (carrier CPS limit) — failed dials still consume
        # carrier capacity — sleep between attempts, never before the first.
        if dial_attempted:
            await asyncio.sleep(dial_pacing_s)
        dial_attempted = True
        try:
            await livekit.create_sip_participant(
                room_name, form.insurance_provider_phone_number, trunk_id
            )
        except OutboundDialError:
            logger.warning("dispatch: outbound dial failed for call %s", call.id)
            with contextlib.suppress(Exception):  # room teardown is best-effort
                await livekit.delete_room(room_name)
            requeued = apply_terminal_call_status(
                call, form, CallStatus.FAILED, tenant_max_retries=tenant.max_retries
            )
            call.ended_at = func.now()
            if requeued:
                form.enqueued_at = func.now()
            session.add(
                CallEvent(
                    tenant_id=tenant_id,
                    call_id=call.id,
                    event_type=CallEventType.STATUS.value,
                    event_value=CallStatus.FAILED.value,
                )
            )
            continue

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

    return dispatched


async def _resolve_provider(session: AsyncSession, form: PatientForm) -> InsuranceProvider | None:
    """The form's ACTIVE insurance provider record, or None.

    A missing/unresolved provider is not an error — working-hours enforcement
    and playbook attach are both opt-in and simply skip when it's None.
    """
    if not form.insurance_provider:
        return None
    return (
        await session.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.name == form.insurance_provider,
                InsuranceProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
