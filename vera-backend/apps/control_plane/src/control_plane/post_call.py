"""Post-call resolution — the system edge OUT of AI_PROCESSING.

A completed call — and a user-canceled one, whose transcript may still carry
extractable data for post-call validation — parks its form in AI_PROCESSING
(`call_closeout.close_call`, in its own committed transaction). This module
then decides the lifecycle's next system transition:

- the verified fraction (``load_verified_fraction`` — required, applicable, collectable
  leaves an AUTHORITATIVE call confirmed; ``completion_pct`` only as a legacy-schema
  fallback) below the tenant's ``retry_fill_threshold`` and retries remaining →
  auto-requeue (``AI_PROCESSING → IN_QUEUE``, consuming the retry budget) —
  feature-gated behind the deployment kill-switch (``settings.form_auto_retry_enabled``,
  default OFF) AND the tenant's own ``auto_retry_enabled``. NEVER taken for a
  user-ended (CANCELED) call — the supervisor who ended it does not want the
  payer redialed;
- otherwise → ``EXCEPTION_REVIEW`` for human review. ``COMPLETED`` is never set
  here — only a reviewer's manual approve reaches it.

This is also the seam where post-call AI work (answer extraction from the
transcript into ``form_answer`` rows, recomputing ``completion_pct``) will slot
in; today the decision runs on the answers the form already carries.

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
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallStatus, FormStatus, ReviewReason
from vera_core.observability.correlation import RoomRef
from vera_core.services.call_lifecycle import no_retry_reason
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.verification import load_verified_fraction

logger = logging.getLogger(__name__)


async def resolve_ai_processing(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    ref: RoomRef,
    *,
    trigger: str,
    actor_label: str = "agent-worker",
    auto_retry_enabled: bool = False,
    review_floor: int = REVIEW_CONFIDENCE_FLOOR,
) -> bool:
    """Resolve *ref*'s form out of AI_PROCESSING. Returns True when the form was
    auto-requeued for a retry call (a dispatch pass should follow either way —
    leaving AI_PROCESSING frees a concurrency slot). The low-fill
    auto-retry edge only runs when feature-gated behind the deployment
    kill-switch (*auto_retry_enabled*, settings ``form_auto_retry_enabled``,
    default off) AND the tenant's own ``auto_retry_enabled`` — otherwise every
    form goes to EXCEPTION_REVIEW."""
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
        # A supervisor-ended or rule-terminated call never auto-retries, whatever the
        # fill — the shared never-redial policy (call_lifecycle.no_retry_reason).
        no_retry = no_retry_reason(call)
        # The fill gate costs four queries, so it is computed only once the free checks have
        # already agreed a retry is possible at all — with the deployment kill-switch off (the
        # default) or a never-redial cause on the call, its answer is never read.
        low_fill = False
        if tenant.allows_auto_retry(auto_retry_enabled) and no_retry is None:
            # ONE gate, ONE number: the eval path compares the verified fraction against this
            # same threshold (post_call_eval), so reading completion_pct here made the decision
            # depend on which consumer closed the call. Computed fresh — the stored
            # verified_pct column is a display value `recompute_form_projection` does not
            # maintain. A `None` fraction means the schema is legacy v1, which declares no
            # reference-number field, so "authoritative" is undefined and completion stands in.
            threshold = float(tenant.retry_fill_threshold)
            fraction = await load_verified_fraction(session, form, floor=review_floor)
            low_fill = (
                fraction < threshold
                if fraction is not None
                else float(form.completion_pct) < threshold * 100
            )
        if low_fill:
            # Auto-retry while retries remain; fall through to human review when exhausted.
            with contextlib.suppress(InvalidTransitionError):
                sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant.max_retries)
                form.enqueued_at = func.now()
                requeued = True
        if not requeued:
            # Stamp WHY so the reviewer isn't left with a blank reason: anything
            # reaching this fallback with no never-redial cause (eval consumer
            # unconfigured, or the sweeper reclaiming a stranded form) was never
            # AI-evaluated.
            reason = no_retry if no_retry is not None else ReviewReason.NOT_EVALUATED
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
            auto_retry_enabled=auto_retry_enabled,
            review_floor=review_floor,
        )
        resolved += 1
        logger.info("post-call sweep: resolved stuck AI_PROCESSING form for call %s", call_id)
    return resolved
