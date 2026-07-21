"""Unit tests for FormStateMachine — transition validation and side effects.

These are pure-logic tests: they check the transition map, guard conditions,
and side-effect assignments without hitting a database.

Note: `enqueued_at` is NOT set by the state machine — it is the caller's
responsibility (using `func.now()`, the DB clock) after a successful IN_QUEUE
transition. The state machine only manages `status` and `retry_count`.
"""

import types
from unittest.mock import MagicMock

import pytest

from vera_core.models.enums import FormStatus
from vera_core.services.form_state_machine import (
    ALLOWED_TRANSITIONS,
    FormStateMachine,
    InvalidTransitionError,
)


def _form(status: FormStatus) -> types.SimpleNamespace:
    return types.SimpleNamespace(status=status.value, retry_count=0, enqueued_at=None)


class TestTransitionMap:
    """Every allowed and disallowed (from, to) pair."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (FormStatus.READY_FOR_PROCESSING, FormStatus.IN_QUEUE),
            (FormStatus.READY_FOR_PROCESSING, FormStatus.EXCEPTION_REVIEW),
            (FormStatus.IN_QUEUE, FormStatus.IN_CALL),
            (FormStatus.IN_QUEUE, FormStatus.EXPIRED),
            # Dispatcher marks an undispatchable (no-plan) form as failed.
            (FormStatus.IN_QUEUE, FormStatus.CALL_FAILED),
            (FormStatus.IN_CALL, FormStatus.AI_PROCESSING),
            (FormStatus.IN_CALL, FormStatus.CALL_FAILED),
            (FormStatus.AI_PROCESSING, FormStatus.EXCEPTION_REVIEW),
            (FormStatus.AI_PROCESSING, FormStatus.IN_QUEUE),
            # The post-call eval completes a form only when no field needs review.
            (FormStatus.AI_PROCESSING, FormStatus.COMPLETED),
            (FormStatus.CALL_FAILED, FormStatus.IN_QUEUE),
            (FormStatus.EXCEPTION_REVIEW, FormStatus.IN_QUEUE),
            (FormStatus.EXCEPTION_REVIEW, FormStatus.COMPLETED),
        ],
    )
    def test_allowed_transitions(self, from_status: FormStatus, to_status: FormStatus) -> None:
        assert to_status in ALLOWED_TRANSITIONS[from_status]

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (FormStatus.COMPLETED, FormStatus.IN_QUEUE),
            (FormStatus.EXPIRED, FormStatus.IN_QUEUE),
            (FormStatus.IN_QUEUE, FormStatus.COMPLETED),
            (FormStatus.IN_CALL, FormStatus.IN_QUEUE),
            # A call ending never completes the form directly — COMPLETED comes
            # from a reviewer's approve or a nothing-to-review post-call eval.
            (FormStatus.IN_CALL, FormStatus.COMPLETED),
            (FormStatus.AI_PROCESSING, FormStatus.CALL_FAILED),
            (FormStatus.READY_FOR_PROCESSING, FormStatus.COMPLETED),
            (FormStatus.READY_FOR_PROCESSING, FormStatus.CALL_FAILED),
        ],
    )
    def test_disallowed_transitions(self, from_status: FormStatus, to_status: FormStatus) -> None:
        assert to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset())


class TestFormStateMachine:
    """Side-effect and guard tests on a mock PatientForm."""

    def _make_form(self, status: FormStatus, retry_count: int = 0) -> MagicMock:
        """Minimal in-memory PatientForm-like object for testing."""
        form = MagicMock()
        form.status = status.value
        form.retry_count = retry_count
        form.enqueued_at = None
        return form

    def test_transition_to_in_queue_does_not_set_enqueued_at(self) -> None:
        """The state machine must NOT set enqueued_at — the DB-clock caller owns it."""
        sm = FormStateMachine()
        form = self._make_form(FormStatus.READY_FOR_PROCESSING)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        # enqueued_at starts as None and must remain unset by the state machine.
        assert form.enqueued_at is None

    def test_transition_in_queue_to_call_failed_spends_no_retry_budget(self) -> None:
        """Marking an undispatchable form CALL_FAILED is not a retry — retry_count is
        untouched (only → IN_QUEUE edges spend the budget)."""
        sm = FormStateMachine()
        form = self._make_form(FormStatus.IN_QUEUE, retry_count=2)
        sm.transition(form, FormStatus.CALL_FAILED, tenant_max_retries=5)
        assert form.status == FormStatus.CALL_FAILED.value
        assert form.retry_count == 2

    def test_transition_call_failed_to_in_queue_increments_retry(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.CALL_FAILED, retry_count=1)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        assert form.retry_count == 2
        # enqueued_at is the caller's responsibility, not the state machine's.
        assert form.enqueued_at is None

    def test_transition_call_failed_to_in_queue_blocked_at_max_retries(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.CALL_FAILED, retry_count=5)
        with pytest.raises(InvalidTransitionError, match="retries exhausted"):
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)

    def test_transition_ai_processing_to_in_queue_increments_retry(self) -> None:
        """The low-completion auto-retry edge is a retry call — it counts against
        the tenant's retry budget exactly like a CALL_FAILED requeue."""
        sm = FormStateMachine()
        form = self._make_form(FormStatus.AI_PROCESSING, retry_count=1)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        assert form.retry_count == 2
        assert form.enqueued_at is None

    def test_transition_ai_processing_to_in_queue_blocked_at_max_retries(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.AI_PROCESSING, retry_count=5)
        with pytest.raises(InvalidTransitionError, match="retries exhausted"):
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)

    def test_manual_requeue_from_call_failed_bypasses_cap_and_resets_budget(self) -> None:
        """An operator's manual enqueue starts a FRESH episode: it neither
        consumes nor is blocked by the auto-retry budget, and it resets the
        counter so the new episode gets its full auto-retry allowance. The cap
        exists to stop the automatic redial loop within one episode — not to
        permanently retire the form."""
        sm = FormStateMachine()
        form = self._make_form(FormStatus.CALL_FAILED, retry_count=5)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5, manual=True)
        assert form.status == FormStatus.IN_QUEUE.value
        assert form.retry_count == 0

    def test_manual_requeue_from_exception_review_resets_budget(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.EXCEPTION_REVIEW, retry_count=5)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5, manual=True)
        assert form.retry_count == 0

    def test_auto_requeue_from_call_failed_still_capped(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.CALL_FAILED, retry_count=5)
        with pytest.raises(InvalidTransitionError, match="retries exhausted"):
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)

    def test_transition_exception_review_to_in_queue_does_not_touch_retry(self) -> None:
        """Manual requeue from review is an operator decision, not a retry —
        it must not consume the auto-retry budget."""
        sm = FormStateMachine()
        form = self._make_form(FormStatus.EXCEPTION_REVIEW, retry_count=5)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        assert form.retry_count == 5

    def test_invalid_transition_raises(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)

    def test_idempotent_same_status_is_noop(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.IN_QUEUE)
        # Same status → no-op, no error
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        assert form.status == FormStatus.IN_QUEUE.value


# ---------------------------------------------------------------------------
# AI_PROCESSING edge cases (merged from vera_core unit tests)
# ---------------------------------------------------------------------------


def test_ai_processing_can_go_to_exception_review() -> None:
    form = _form(FormStatus.AI_PROCESSING)
    FormStateMachine().transition(form, FormStatus.EXCEPTION_REVIEW, tenant_max_retries=5)
    assert form.status == FormStatus.EXCEPTION_REVIEW.value


def test_ai_processing_can_still_complete() -> None:
    form = _form(FormStatus.AI_PROCESSING)
    FormStateMachine().transition(form, FormStatus.COMPLETED, tenant_max_retries=5)
    assert form.status == FormStatus.COMPLETED.value


def test_ready_cannot_jump_to_exception_review_via_ai_processing() -> None:
    form = _form(FormStatus.IN_QUEUE)
    with pytest.raises(InvalidTransitionError):
        FormStateMachine().transition(form, FormStatus.EXCEPTION_REVIEW, tenant_max_retries=5)


def test_ai_processing_to_in_queue_retries_and_increments() -> None:
    form = _form(FormStatus.AI_PROCESSING)
    form.retry_count = 0
    FormStateMachine().transition(form, FormStatus.IN_QUEUE, tenant_max_retries=3)
    assert form.status == FormStatus.IN_QUEUE.value and form.retry_count == 1


def test_ai_processing_to_in_queue_blocked_when_cap_hit() -> None:
    form = _form(FormStatus.AI_PROCESSING)
    form.retry_count = 3
    with pytest.raises(InvalidTransitionError):
        FormStateMachine().transition(form, FormStatus.IN_QUEUE, tenant_max_retries=3)
