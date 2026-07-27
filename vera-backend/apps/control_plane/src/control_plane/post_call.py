"""Post-call resolution — the system edge OUT of AI_PROCESSING.

A completed call — and a user-canceled one, whose transcript may still carry
extractable data for post-call validation — parks its form in AI_PROCESSING
(`call_closeout.close_call`, in its own committed transaction). This module
then decides the lifecycle's next system transition:

- completion below the tenant's ``retry_fill_threshold`` and retries remaining
  → auto-requeue (``AI_PROCESSING → IN_QUEUE``, consuming the retry budget) —
  feature-gated behind ``auto_retry_enabled`` (default OFF) until a post-call
  form-filling mechanism exists, since today nothing raises ``completion_pct``
  between calls and a retry would redial to no benefit. NEVER taken for a
  user-ended (CANCELED) call — the supervisor who ended it does not want the
  payer redialed;
- otherwise → ``EXCEPTION_REVIEW`` for human review. ``COMPLETED`` is never set
  here — only a reviewer's manual approve reaches it.

This is also the seam where post-call AI work (answer extraction from the
transcript into ``form_answer`` rows, recomputing ``completion_pct``) will slot
in; today the decision runs on the completion the form already carries.

Idempotent: a form no longer in AI_PROCESSING is left untouched (redelivered
``call.ended`` events and sweeper/consumer races are harmless — the row lock
serializes them). Because closeout and resolution are separate transactions, a
crash between them strands the form in AI_PROCESSING, leaking a concurrency
slot (the dispatcher counts AI_PROCESSING as active); the pipeline sweeper
closes that hole via `sweep_stuck_ai_processing`.
"""

import contextlib
import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit import AuditRecord, AuditSink
from vera_core.db.rls import tenant_session
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallStatus, FormStatus, ReviewReason
from vera_core.observability.correlation import RoomRef
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError

logger = logging.getLogger(__name__)


async def resolve_ai_processing(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    ref: RoomRef,
    *,
    trigger: str,
    actor_label: str = "agent-worker",
    auto_retry_enabled: bool = False,
) -> bool:
    """Resolve *ref*'s form out of AI_PROCESSING. Returns True when the form was
    auto-requeued for a retry call (a dispatch pass should follow either way —
    leaving AI_PROCESSING frees a concurrency slot). The low-completion
    auto-retry edge only runs when *auto_retry_enabled* (settings
    ``form_auto_retry_enabled``, default off) — otherwise every form goes to
    EXCEPTION_REVIEW."""
    async with tenant_session(sessionmaker, ref.tenant_id) as session:
        call = (
            await session.execute(select(Call).where(Call.id == ref.call_id))
        ).scalar_one_or_none()
        if call is None:
            return False  # voice-lab room — no pipeline form to resolve
        form = (
            await session.execute(
                select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
            )
        ).scalar_one_or_none()
        if form is None or form.status != FormStatus.AI_PROCESSING.value:
            return False  # form deleted, or already resolved (idempotent redelivery)
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == ref.tenant_id))
        ).scalar_one()

        # completion_pct is already current here: the Observer's ai_call answers are
        # stream-ordered before call.ended and, under the consumer's per-room sequential
        # dispatch, each recomputed the projection as it landed — so this reads the fresh
        # value with no recompute needed on this path.
        sm = FormStateMachine()
        requeued = False
        # A user-ended (CANCELED) call never auto-retries, whatever the fill:
        # the supervisor who ended it does not want the payer redialed. The
        # end-intent stamp is checked too, in case a resolver races the
        # closeout's status write.
        user_ended = (
            call.current_status == CallStatus.CANCELED.value or call.end_requested_by_id is not None
        )
        # completion_pct is 0-100; retry_fill_threshold is a 0-1 fraction.
        low_fill = float(form.completion_pct) < float(tenant.retry_fill_threshold) * 100
        if auto_retry_enabled and low_fill and not user_ended:
            # Auto-retry while retries remain; fall through to human review when exhausted.
            with contextlib.suppress(InvalidTransitionError):
                sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant.max_retries)
                form.enqueued_at = func.now()
                requeued = True
        if not requeued:
            # Stamp WHY so the reviewer isn't left with a blank reason: a
            # supervisor-ended call is USER_ENDED; anything else reaching this
            # fallback (eval consumer unconfigured, or the sweeper reclaiming a
            # stranded form) was never AI-evaluated.
            reason = ReviewReason.USER_ENDED if user_ended else ReviewReason.NOT_EVALUATED
            sm.transition(
                form,
                FormStatus.EXCEPTION_REVIEW,
                tenant_max_retries=tenant.max_retries,
                reason=reason,
            )

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
                    "from": FormStatus.AI_PROCESSING.value,
                    "to": form.status,
                    "call_id": str(call.id),
                    "trigger": trigger,
                },
            )
        )
    return requeued


async def sweep_stuck_ai_processing(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    tenant_id: UUID,
    *,
    grace_s: int,
    auto_retry_enabled: bool = False,
) -> int:
    """Resolve forms stranded in AI_PROCESSING (a crash between closeout and
    resolution) whose call ended more than *grace_s* ago. Both AI_PROCESSING
    writers are covered — COMPLETED and CANCELED closeouts. Returns the number
    of forms resolved — each freed a leaked concurrency slot, so the caller
    should run a dispatch pass when it's non-zero."""
    grace = func.make_interval(0, 0, 0, 0, 0, 0, grace_s)
    async with tenant_session(sessionmaker, tenant_id) as session:
        stuck_call_ids = (
            (
                await session.execute(
                    select(Call.id)
                    .join(PatientForm, PatientForm.id == Call.form_id)
                    .where(
                        Call.tenant_id == tenant_id,
                        Call.current_status.in_(
                            [CallStatus.COMPLETED.value, CallStatus.CANCELED.value]
                        ),
                        Call.ended_at < func.now() - grace,
                        PatientForm.status == FormStatus.AI_PROCESSING.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    resolved = 0
    for call_id in stuck_call_ids:
        ref = RoomRef(tenant_id=tenant_id, call_id=call_id)
        await resolve_ai_processing(
            sessionmaker,
            audit,
            ref,
            trigger="sweeper_ai_processing",
            actor_label="pipeline-sweeper",
            auto_retry_enabled=auto_retry_enabled,
        )
        resolved += 1
        logger.info("post-call sweep: resolved stuck AI_PROCESSING form for call %s", call_id)
    return resolved
