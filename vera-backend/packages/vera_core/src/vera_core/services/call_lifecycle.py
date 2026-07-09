"""Terminal call-status application — the one place a call's terminal status drives
the form lifecycle. Used by the queue dispatcher (dial failure) and the control
plane's worker-event consumer (call.ended / call.failed).

The form edge is best-effort by design: an illegal transition (e.g. a second call
racing the same form) must not prevent the call's terminal status from being
recorded — mirror of the old callback endpoint's contract.
"""

import contextlib
import logging
from typing import Any

from vera_core.models.enums import CallStatus, FormStatus
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError

logger = logging.getLogger(__name__)

_FORM_EDGE: dict[CallStatus, FormStatus] = {
    CallStatus.COMPLETED: FormStatus.COMPLETED,
    CallStatus.FAILED: FormStatus.CALL_FAILED,
    CallStatus.NO_ANSWER: FormStatus.CALL_FAILED,
    CallStatus.BUSY: FormStatus.CALL_FAILED,
}


def apply_terminal_call_status(
    call: Any, form: Any, status: CallStatus, *, tenant_max_retries: int
) -> bool:
    """Record *status* on *call* and drive *form*'s lifecycle edge.

    Returns True when the form was auto-requeued for retry — the caller owns
    `form.enqueued_at` (DB clock) in that case.
    """
    if status not in _FORM_EDGE:
        raise ValueError(f"{status.value} is not a terminal call status")
    call.current_status = status.value
    sm = FormStateMachine()
    requeued = False
    try:
        sm.transition(form, _FORM_EDGE[status], tenant_max_retries=tenant_max_retries)
        if _FORM_EDGE[status] is FormStatus.CALL_FAILED:
            # Auto-retry while retries remain; silently stay CALL_FAILED when exhausted.
            with contextlib.suppress(InvalidTransitionError):
                sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant_max_retries)
                requeued = True
    except InvalidTransitionError:
        logger.warning(
            "terminal call status '%s': form cannot leave '%s'; call status recorded, "
            "form left unchanged",
            status.value,
            form.status,
        )
    return requeued
