"""Post-call resolution — the system edge OUT of AI_PROCESSING.

A completed call — and a user-canceled one, whose transcript may still carry
extractable data for post-call validation — parks its form in AI_PROCESSING
(`call_closeout.close_call`, in its own committed transaction). This module
then moves the form out of it — ALWAYS to ``EXCEPTION_REVIEW``, never to
``IN_QUEUE`` and never to ``COMPLETED`` (only a reviewer's manual approve
reaches that). It makes no retry decision: this module runs precisely when
nothing evaluated the call, and a fill-based decision needs a judge verdict it
never has. ``retry_decision.decide_retry``, reached from
``post_call_eval.evaluate_call``, is the one place that decision is made — see
``resolve_ai_processing`` for why gating on the verified fraction here redialed
every form until ``max_retries``.

What this module guarantees is that the form LEAVES ``AI_PROCESSING`` carrying an
honest ``review_reason``, so a crash between closeout and resolution cannot
strand it and leak a concurrency slot.

Idempotent: a form no longer in AI_PROCESSING is left untouched (redelivered
``call.ended`` events and sweeper/consumer races are harmless — the row lock
serializes them). Because closeout and resolution are separate transactions, a
crash between them strands the form in AI_PROCESSING, leaking a concurrency
slot (the dispatcher counts AI_PROCESSING as active); the pipeline sweeper
closes that hole via `sweep_stuck_ai_processing`.
"""

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit import AuditRecord, AuditSink
from vera_core.db.rls import tenant_session
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallStatus, FormStatus, ReviewReason
from vera_core.observability.correlation import RoomRef
from vera_core.services.call_lifecycle import no_retry_reason
from vera_core.services.form_state_machine import FormStateMachine

logger = logging.getLogger(__name__)


async def resolve_ai_processing(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    ref: RoomRef,
    *,
    trigger: str,
    actor_label: str = "agent-worker",
) -> None:
    """Resolve *ref*'s form out of AI_PROCESSING, always into EXCEPTION_REVIEW.

    This resolver does not auto-retry. It runs only when nothing evaluated the call — the
    eval consumer is unwired, or the sweeper is reclaiming a form `evaluate_call` never
    resolved — and a fill-based retry decision needs a judge verdict this path never has.
    `retry_decision.decide_retry`, reached from `post_call_eval.evaluate_call`, is the one
    place that decision is made.

    A dispatch pass should still follow: leaving AI_PROCESSING frees a concurrency slot."""
    async with tenant_session(sessionmaker, ref.tenant_id) as session:
        call = (
            await session.execute(select(Call).where(Call.id == ref.call_id))
        ).scalar_one_or_none()
        if call is None:
            return  # voice-lab room — no pipeline form to resolve
        form = (
            await session.execute(
                select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
            )
        ).scalar_one_or_none()
        if form is None or form.status != FormStatus.AI_PROCESSING.value:
            return  # form deleted, or already resolved (idempotent redelivery)
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == ref.tenant_id))
        ).scalar_one()

        sm = FormStateMachine()
        # This resolver NEVER auto-retries, and that is deliberate. `is_call_confirmed`
        # requires a judge verdict (`ai_supported`), and this path runs precisely when no
        # judge ran — the eval consumer is unwired, or the sweeper is reclaiming a form the
        # eval never resolved. So the verified fraction it used to gate on was structurally
        # 0.0 here for EVERY call however good, and 0.0 is below every threshold: with
        # auto-retry on, this redialled every form until max_retries, against real payers.
        # A retry decision needs evidence about fill; without a judge there is none. The one
        # decision lives in `retry_decision.decide_retry`, called only from the eval path.
        #
        # What remains is this module's real job: guarantee the form leaves AI_PROCESSING with
        # an honest reason, so a crash between closeout and resolution cannot strand it.
        reason = no_retry_reason(call) or ReviewReason.NOT_EVALUATED
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


async def sweep_stuck_ai_processing(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    tenant_id: UUID,
    *,
    grace_s: int,
    auto_retry_enabled: bool = False,
    review_floor: int = REVIEW_CONFIDENCE_FLOOR,
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
        )
        resolved += 1
        logger.info("post-call sweep: resolved stuck AI_PROCESSING form for call %s", call_id)
    return resolved
