"""apply_terminal_call_status — terminal call statuses drive the form lifecycle."""

from decimal import Decimal
from types import SimpleNamespace

from vera_core.models.enums import CallStatus, FormStatus
from vera_core.services.call_lifecycle import apply_terminal_call_status

_FORM_COMPLETION_PCT = Decimal("62.50")


def _call() -> SimpleNamespace:
    return SimpleNamespace(current_status=CallStatus.ACTIVE.value, completion_pct=Decimal("0"))


def _form(status: FormStatus = FormStatus.IN_CALL, retry_count: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        status=status.value,
        retry_count=retry_count,
        enqueued_at=None,
        completion_pct=_FORM_COMPLETION_PCT,
    )


def test_completed_call_moves_form_to_ai_processing() -> None:
    """A completed call hands the form to post-call processing — never straight
    to COMPLETED (that requires manual approval from EXCEPTION_REVIEW)."""
    call, form = _call(), _form()
    requeued = apply_terminal_call_status(
        call, form, CallStatus.COMPLETED, tenant_max_retries=3, auto_retry_enabled=True
    )
    assert call.current_status == CallStatus.COMPLETED.value
    assert form.status == FormStatus.AI_PROCESSING.value
    assert requeued is False


def test_failed_call_auto_requeues_with_retries_remaining() -> None:
    call, form = _call(), _form(retry_count=0)
    requeued = apply_terminal_call_status(
        call, form, CallStatus.NO_ANSWER, tenant_max_retries=3, auto_retry_enabled=True
    )
    assert call.current_status == CallStatus.NO_ANSWER.value
    assert form.status == FormStatus.IN_QUEUE.value
    assert form.retry_count == 1
    assert requeued is True


def test_failed_call_not_requeued_when_auto_retry_disabled() -> None:
    """Tenant auto-retry OFF suppresses the failed/no-answer redial too: the form
    parks in CALL_FAILED and is never requeued, even with the full budget remaining."""
    call, form = _call(), _form(retry_count=0)
    requeued = apply_terminal_call_status(
        call, form, CallStatus.NO_ANSWER, tenant_max_retries=5, auto_retry_enabled=False
    )
    assert call.current_status == CallStatus.NO_ANSWER.value
    assert form.status == FormStatus.CALL_FAILED.value
    assert form.retry_count == 0  # budget untouched — no clinical retry was spent
    assert requeued is False


def test_failed_call_stays_call_failed_when_retries_exhausted() -> None:
    call, form = _call(), _form(retry_count=3)
    requeued = apply_terminal_call_status(
        call, form, CallStatus.BUSY, tenant_max_retries=3, auto_retry_enabled=True
    )
    assert form.status == FormStatus.CALL_FAILED.value
    assert requeued is False


def test_canceled_is_terminal() -> None:
    from vera_core.models.call import TERMINAL_CALL_STATUSES

    assert CallStatus.CANCELED in TERMINAL_CALL_STATUSES


def test_canceled_rides_the_post_call_pipeline_without_retry() -> None:
    """User-requested end: the transcript may still carry extractable data, so
    the form rides the normal post-call pipeline (AI_PROCESSING) instead of
    parking at CALL_FAILED — and is never auto-requeued here, even with every
    retry remaining (resolve_ai_processing enforces the same for its edge)."""
    call, form = _call(), _form(retry_count=0)
    requeued = apply_terminal_call_status(
        call, form, CallStatus.CANCELED, tenant_max_retries=5, auto_retry_enabled=True
    )
    assert call.current_status == CallStatus.CANCELED.value
    assert form.status == FormStatus.AI_PROCESSING.value
    assert form.retry_count == 0
    assert requeued is False


def test_illegal_form_edge_still_records_call_status() -> None:
    call, form = _call(), _form(status=FormStatus.COMPLETED)  # form already terminal
    requeued = apply_terminal_call_status(
        call, form, CallStatus.FAILED, tenant_max_retries=3, auto_retry_enabled=True
    )
    assert call.current_status == CallStatus.FAILED.value
    assert form.status == FormStatus.COMPLETED.value  # untouched
    assert requeued is False


def test_terminal_status_freezes_form_completion_onto_the_call() -> None:
    call, form = _call(), _form()
    apply_terminal_call_status(
        call, form, CallStatus.COMPLETED, tenant_max_retries=3, auto_retry_enabled=True
    )
    assert call.completion_pct == _FORM_COMPLETION_PCT


def test_freeze_happens_even_when_the_form_edge_is_illegal() -> None:
    """The form edge is best-effort by design; the call's snapshot must not be."""
    call, form = _call(), _form(status=FormStatus.COMPLETED)  # form already terminal
    apply_terminal_call_status(
        call, form, CallStatus.COMPLETED, tenant_max_retries=3, auto_retry_enabled=True
    )
    assert call.current_status == CallStatus.COMPLETED.value
    assert call.completion_pct == _FORM_COMPLETION_PCT
