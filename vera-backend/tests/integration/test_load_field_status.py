"""Integration test for load_field_status — PHI-free DB helper."""

import pytest

from tests.integration.conftest import _FieldStatusCtx
from vera_core.services.field_status import load_field_status


@pytest.mark.asyncio
async def test_load_field_status_maps_source_conf_supported(
    seeded_form_with_answers: _FieldStatusCtx,
) -> None:
    # fixture: form with ai_call(path=cov.a, conf=55, supported=False) + human(path=cov.b)
    ctx = seeded_form_with_answers
    status = await load_field_status(ctx.session, ctx.form_id)
    # cov.a: ai_call answer with confidence=55
    assert status["cov.a"].source == "ai_call"
    assert status["cov.a"].ai_confidence == 55
    assert status["cov.a"].ai_supported is False
    assert status["cov.a"].filled is True
    # cov.b: human answer with no evaluation
    assert status["cov.b"].source == "human"
    assert status["cov.b"].ai_supported is None
