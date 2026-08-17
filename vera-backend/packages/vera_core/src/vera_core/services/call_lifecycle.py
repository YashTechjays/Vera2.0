"""Terminal call-status application — the one place a call's terminal status drives
the form lifecycle. Used by the queue dispatcher (dial failure) and the control
plane's worker-event consumer (call.ended / call.failed).

The form edge is best-effort by design: an illegal transition (e.g. a second call
racing the same form) must not prevent the call's terminal status from being
recorded — mirror of the old callback endpoint's contract.
"""

import logging
from typing import Any

from vera_core.models.enums import CallStatus, FormStatus, ReviewReason
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError

logger = logging.getLogger(__name__)


def no_retry_reason(call: Any) -> ReviewReason | None:
    """Why *call*'s form must never be auto-redialed, or None when the ordinary retry gates
    decide — the one encoding of that policy, shared by both post-call resolvers."""
    # A supervisor's own end is checked first: the more specific human decision.
    if call.current_status == CallStatus.CANCELED.value or call.end_requested_by_id is not None:
        return ReviewReason.USER_ENDED
    if call.terminated_by_flow_rule:
        return ReviewReason.TERMINATED_BY_RULE
    return None


# A completed call parks the form in AI_PROCESSING — post-call resolution
# (control_plane.post_call) then drives the EXCEPTION_REVIEW / auto-retry edge.
# The form never goes straight to COMPLETED: that is the reviewer's manual edge.
_FORM_EDGE: dict[CallStatus, FormStatus] = {
    CallStatus.COMPLETED: FormStatus.AI_PROCESSING,
    CallStatus.FAILED: FormStatus.CALL_FAILED,
    CallStatus.NO_ANSWER: FormStatus.CALL_FAILED,
    CallStatus.BUSY: FormStatus.CALL_FAILED,
    # User-requested end: the call's transcript may still carry extractable
    # data, so the form rides the normal post-call pipeline (AI_PROCESSING →
    # EXCEPTION_REVIEW) instead of parking at CALL_FAILED. The pipeline's
    # resolver suppresses auto-retry for a canceled call — a supervisor who
    # ended the call never wants the number redialed.
    CallStatus.CANCELED: FormStatus.AI_PROCESSING,
}


def fail_and_requeue(form: Any, *, tenant_max_retries: int, auto_retry_enabled: bool) -> bool:
    """Park *form* in CALL_FAILED, then auto-retry it while retries remain.

    Returns True when the form was requeued — the caller owns `form.enqueued_at`
    (DB clock) in that case. The one encoding of the failed-call retry edge, reached
    only from a terminal call status: a call connected and ended badly, so retrying is
    a clinical decision and spends the tenant's budget. A dispatch-prep failure never
    dialed and deliberately does NOT come here — it parks without spending
    (`queue_dispatcher`), so an infrastructure blip cannot retire a patient's form.

    *auto_retry_enabled* is the tenant's own toggle (not the deployment kill-switch):
    OFF means no redial of any kind, so the form parks in CALL_FAILED without
    spending budget — mirroring the post-call eval path's tenant gate.
    """
    sm = FormStateMachine()
    sm.transition(form, FormStatus.CALL_FAILED, tenant_max_retries=tenant_max_retries)
    if not auto_retry_enabled or not sm.can_retry(form, tenant_max_retries=tenant_max_retries):
        return False
    sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant_max_retries)
    return True


def apply_terminal_call_status(
    call: Any, form: Any, status: CallStatus, *, tenant_max_retries: int, auto_retry_enabled: bool
) -> bool:
    """Record *status* on *call* and drive *form*'s lifecycle edge.

    Returns True when the form was auto-requeued for retry — the caller owns
    `form.enqueued_at` (DB clock) in that case. *auto_retry_enabled* is the tenant's
    toggle; OFF suppresses the failed-call redial (see `fail_and_requeue`).
    """
    if status not in _FORM_EDGE:
        raise ValueError(f"{status.value} is not a terminal call status")
    call.current_status = status.value
    # Snapshot, not a live read: the form keeps evolving (edits, retries); history must not.
    call.completion_pct = form.completion_pct
    requeued = False
    try:
        if _FORM_EDGE[status] is FormStatus.CALL_FAILED:
            # A rule-terminated call is never redialed, whatever closed it (e.g. the
            # sweeper closing a crashed worker's room as FAILED): a redial would only
            # re-reach the same terminal answer (VR2-188).
            requeued = fail_and_requeue(
                form,
                tenant_max_retries=tenant_max_retries,
                auto_retry_enabled=auto_retry_enabled and not call.terminated_by_flow_rule,
            )
        else:
            FormStateMachine().transition(
                form, _FORM_EDGE[status], tenant_max_retries=tenant_max_retries
            )
    except InvalidTransitionError:
        logger.warning(
            "terminal call status '%s': form cannot leave '%s'; call status recorded, "
            "form left unchanged",
            status.value,
            form.status,
        )
    return requeued
