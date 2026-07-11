"""apply_terminal_call_status — terminal call statuses drive the form lifecycle."""

from types import SimpleNamespace

from vera_core.models.enums import CallStatus, FormStatus
from vera_core.services.call_lifecycle import apply_terminal_call_status


def _call() -> SimpleNamespace:
    return SimpleNamespace(current_status=CallStatus.ACTIVE.value)


def _form(status: FormStatus = FormStatus.IN_CALL, retry_count: int = 0) -> SimpleNamespace:
    return SimpleNamespace(status=status.value, retry_count=retry_count, enqueued_at=None)


def test_completed_call_moves_form_to_ai_processing() -> None:
    """A completed call hands the form to post-call processing — never straight
    to COMPLETED (that requires manual approval from EXCEPTION_REVIEW)."""
    call, form = _call(), _form()
    requeued = apply_terminal_call_status(call, form, CallStatus.COMPLETED, tenant_max_retries=3)
    assert call.current_status == CallStatus.COMPLETED.value
    assert form.status == FormStatus.AI_PROCESSING.value
    assert requeued is False


def test_failed_call_auto_requeues_with_retries_remaining() -> None:
    call, form = _call(), _form(retry_count=0)
    requeued = apply_terminal_call_status(call, form, CallStatus.NO_ANSWER, tenant_max_retries=3)
    assert call.current_status == CallStatus.NO_ANSWER.value
    assert form.status == FormStatus.IN_QUEUE.value
    assert form.retry_count == 1
    assert requeued is True


def test_failed_call_stays_call_failed_when_retries_exhausted() -> None:
    call, form = _call(), _form(retry_count=3)
    requeued = apply_terminal_call_status(call, form, CallStatus.BUSY, tenant_max_retries=3)
    assert form.status == FormStatus.CALL_FAILED.value
    assert requeued is False


def test_canceled_is_terminal() -> None:
    from vera_core.models.call import TERMINAL_CALL_STATUSES

    assert CallStatus.CANCELED in TERMINAL_CALL_STATUSES


def test_canceled_parks_form_without_retry() -> None:
    """User-requested end: the form parks at CALL_FAILED for a human — never
    auto-requeued, even with every retry remaining."""
    call, form = _call(), _form(retry_count=0)
    requeued = apply_terminal_call_status(call, form, CallStatus.CANCELED, tenant_max_retries=5)
    assert call.current_status == CallStatus.CANCELED.value
    assert form.status == FormStatus.CALL_FAILED.value
    assert form.retry_count == 0
    assert requeued is False


def test_illegal_form_edge_still_records_call_status() -> None:
    call, form = _call(), _form(status=FormStatus.COMPLETED)  # form already terminal
    requeued = apply_terminal_call_status(call, form, CallStatus.FAILED, tenant_max_retries=3)
    assert call.current_status == CallStatus.FAILED.value
    assert form.status == FormStatus.COMPLETED.value  # untouched
    assert requeued is False
