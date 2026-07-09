import pytest
from vera_core.integrations.llm import (
    ExtractedField,
    FakeLLMClient,
    JudgeVerdict,
    TranscriptTurn,
)


@pytest.mark.asyncio
async def test_fake_llm_returns_canned_results():
    extracted = [ExtractedField("sections.cov.network_status", "in-network", 90, 2)]
    verdicts = [JudgeVerdict("sections.cov.network_status", True, 88, "in network")]
    client = FakeLLMClient(extracted=extracted, verdicts=verdicts)

    turns = [TranscriptTurn(2, "user", "you are in network")]
    assert await client.extract(field_paths=["sections.cov.network_status"], turns=turns) == extracted
    assert await client.judge(extracted=extracted, turns=turns) == verdicts
