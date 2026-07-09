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
# legal target statuses from that source.
ALLOWED_TRANSITIONS: dict[FormStatus, frozenset[FormStatus]] = {
    FormStatus.READY_FOR_PROCESSING: frozenset({FormStatus.IN_QUEUE, FormStatus.EXCEPTION_REVIEW}),
    FormStatus.IN_QUEUE: frozenset({FormStatus.IN_CALL, FormStatus.EXPIRED}),
    # The worker reports a terminal COMPLETED directly from IN_CALL; AI_PROCESSING
    # is a reserved intermediate for post-call processing that no code path sets yet.
    FormStatus.IN_CALL: frozenset(
        {FormStatus.AI_PROCESSING, FormStatus.COMPLETED, FormStatus.CALL_FAILED}
    ),
    FormStatus.AI_PROCESSING: frozenset(
        {FormStatus.COMPLETED, FormStatus.CALL_FAILED, FormStatus.EXCEPTION_REVIEW}
    ),
    FormStatus.CALL_FAILED: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.EXCEPTION_REVIEW: frozenset({FormStatus.IN_QUEUE, FormStatus.COMPLETED}),
}


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
            The tenant's ``max_retries`` cap — guards ``CALL_FAILED → IN_QUEUE``.

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

        # Guard: retry cap on CALL_FAILED → IN_QUEUE.
        if current == FormStatus.CALL_FAILED and target == FormStatus.IN_QUEUE:
            if form.retry_count >= tenant_max_retries:
                raise InvalidTransitionError(
                    current.value, target.value, reason="retries exhausted"
                )
            form.retry_count += 1

        form.status = target.value
