import types

import pytest

from vera_core.models.enums import FormStatus
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError


def _form(status: FormStatus):
    return types.SimpleNamespace(status=status.value, retry_count=0, enqueued_at=None)


def test_ai_processing_can_go_to_exception_review():
    form = _form(FormStatus.AI_PROCESSING)
    FormStateMachine().transition(form, FormStatus.EXCEPTION_REVIEW, tenant_max_retries=5)
    assert form.status == FormStatus.EXCEPTION_REVIEW.value


def test_ai_processing_can_still_complete():
    form = _form(FormStatus.AI_PROCESSING)
    FormStateMachine().transition(form, FormStatus.COMPLETED, tenant_max_retries=5)
    assert form.status == FormStatus.COMPLETED.value


def test_ready_cannot_jump_to_exception_review_via_ai_processing():
    form = _form(FormStatus.IN_QUEUE)
    with pytest.raises(InvalidTransitionError):
        FormStateMachine().transition(form, FormStatus.EXCEPTION_REVIEW, tenant_max_retries=5)
