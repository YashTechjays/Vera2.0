"""Form lifecycle state machine.

Validates transitions, applies side effects (retry_count), and guards
conditional edges (retry cap). Every form status change in the codebase
MUST go through `FormStateMachine.transition()`.

Note: `enqueued_at` is NOT set here — callers with DB session access set it
to `func.now()` (the DB clock) immediately after a successful IN_QUEUE
transition, per the project's requirement that timestamps come from Postgres.
"""

from typing import Any

from vera_core.models.enums import FormStatus

# The full transition map. Keys are source statuses; values are the set of
# legal target statuses from that source. COMPLETED is reachable only from
# EXCEPTION_REVIEW (manual approve, no disputes) — a call ending never completes
# the form directly; it parks it in AI_PROCESSING for post-call resolution.
ALLOWED_TRANSITIONS: dict[FormStatus, frozenset[FormStatus]] = {
    FormStatus.READY_FOR_PROCESSING: frozenset({FormStatus.IN_QUEUE, FormStatus.EXCEPTION_REVIEW}),
    FormStatus.IN_QUEUE: frozenset(
        {FormStatus.IN_CALL, FormStatus.EXPIRED, FormStatus.CALL_FAILED}
    ),
    FormStatus.IN_CALL: frozenset({FormStatus.AI_PROCESSING, FormStatus.CALL_FAILED}),
    # → EXCEPTION_REVIEW is the terminal post-call parking spot (even an
    # all-satisfied form goes here for human sign-off — the pipeline never
    # auto-COMPLETEs); → IN_QUEUE is the system auto-retry on low completion
    # (guarded by the retry cap below). COMPLETED is deliberately NOT reachable
    # here — it is a human-only edge out of EXCEPTION_REVIEW.
    FormStatus.AI_PROCESSING: frozenset({FormStatus.EXCEPTION_REVIEW, FormStatus.IN_QUEUE}),
    FormStatus.CALL_FAILED: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.EXCEPTION_REVIEW: frozenset({FormStatus.IN_QUEUE, FormStatus.COMPLETED}),
}

# Sources whose → IN_QUEUE edge is a *retry* (consumes the tenant's retry budget).
# EXCEPTION_REVIEW → IN_QUEUE is deliberately absent: a manual requeue is an
# operator decision, not a retry.
_RETRY_SOURCES = frozenset({FormStatus.CALL_FAILED, FormStatus.AI_PROCESSING})


class InvalidTransitionError(Exception):
    """Raised when a form status transition is not allowed."""

    def __init__(self, from_status: str, to_status: str, reason: str = "") -> None:
        detail = f": {reason}" if reason else ""
        super().__init__(f"cannot transition from '{from_status}' to '{to_status}'{detail}")
        self.from_status = from_status
        self.to_status = to_status


class FormStateMachine:
    """Validates and applies form status transitions with side effects."""

    def transition(
        self,
        form: Any,
        target: FormStatus,
        *,
        tenant_max_retries: int,
        manual: bool = False,
        reason: str | None = None,
    ) -> None:
        """Move *form* to *target* status, applying side effects.

        Parameters
        ----------
        form:
            A ``PatientForm`` instance (or mock with `.status`, `.retry_count`,
            `.enqueued_at` attributes).
        target:
            The desired new ``FormStatus``.
        tenant_max_retries:
            The tenant's ``max_retries`` cap — guards the retry edges
            (``CALL_FAILED → IN_QUEUE`` and ``AI_PROCESSING → IN_QUEUE``).
        manual:
            True when an operator (not the system) drives the transition. The
            retry cap bounds the AUTOMATIC redial loop within one enqueue
            episode; a manual enqueue starts a fresh episode — it is never
            blocked by the cap and resets ``retry_count`` so the new episode
            gets its full auto-retry allowance.
        reason:
            Why the pipeline routed the form to ``EXCEPTION_REVIEW`` (a
            ``ReviewReason`` value). Stamped onto ``form.review_reason`` on that
            target and cleared on every other — the machine owns the lifecycle of
            this column so callers can't leave a stale reason behind.

        Raises
        ------
        InvalidTransitionError
            If the transition is illegal or a guard blocks it.
        """
        current = FormStatus(form.status)

        # Idempotent no-op.
        if current == target:
            return

        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise InvalidTransitionError(current.value, target.value)

        if target == FormStatus.IN_QUEUE:
            if manual:
                # Operator decision: fresh episode, fresh auto-retry budget.
                form.retry_count = 0
            elif current in _RETRY_SOURCES:
                # Guard: retry cap on the automatic retry edges into IN_QUEUE.
                if form.retry_count >= tenant_max_retries:
                    raise InvalidTransitionError(
                        current.value, target.value, reason="retries exhausted"
                    )
                form.retry_count += 1

        form.status = target.value
        # The machine owns review_reason's lifecycle: stamped entering
        # EXCEPTION_REVIEW, cleared on every other target — a caller can never
        # leave a stale reason behind on a form that moved on.
        form.review_reason = reason if target == FormStatus.EXCEPTION_REVIEW else None

    def can_retry(self, form: Any, *, tenant_max_retries: int) -> bool:
        """True when the retry-cap guard would allow another retry (IN_QUEUE hop).
        The one encoding of the cap comparison — callers deciding retry-vs-review
        use this instead of re-implementing it."""
        return bool(form.retry_count < tenant_max_retries)
