"""Integration test for load_field_status — PHI-free DB helper."""

import pytest

from vera_core.services.field_status import load_field_status

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_load_field_status_maps_source_conf_supported(
    seeded_form_with_answers,
) -> None:
    # fixture: form with ai_call(path=cov.a, conf=55, supported=False) + human(path=cov.b)
    ctx = seeded_form_with_answers
    status = await load_field_status(ctx.session, ctx.form_id)
    assert status["cov.a"].source == "ai_call" and status["cov.a"].ai_confidence == 55
    assert status["cov.a"].ai_supported is False and status["cov.a"].filled is True
    assert status["cov.b"].source == "human" and status["cov.b"].ai_supported is None
