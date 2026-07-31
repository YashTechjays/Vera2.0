from typing import Any, cast

import pytest

from control_plane.llm import (
    _JUDGE_MAX_ATTEMPTS,
    VertexLLMClient,
    build_extract_prompt,
    build_judge_prompt,
    parse_extract_response,
    parse_judge_response,
)
from vera_core.integrations.llm import ExtractedField, TranscriptTurn

_loads_response = VertexLLMClient._loads_response


def _ef(path: str) -> ExtractedField:
    return ExtractedField(field_path=path, value="yes", confidence=80, evidence_seq=0)


def _vd(path: str) -> dict[str, Any]:
    return {"field_path": path, "supported": True, "confidence": 90, "evidence": "ok"}


class _StubJudgeClient(VertexLLMClient):
    """VertexLLMClient with the Gemini call stubbed — each _generate returns the
    next queued response and records the (prompt, schema) it was called with."""

    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = list(responses)
        self._model = "fake-model"
        self.generate_calls: list[tuple[str, dict[str, Any]]] = []

    async def _generate(self, prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
        self.generate_calls.append((prompt, schema))
        return self._responses.pop(0) if self._responses else []


def _enum_of(call: tuple[str, dict[str, Any]]) -> list[str]:
    return cast(list[str], call[1]["items"]["properties"]["field_path"]["enum"])


def test_extract_prompt_numbers_turns_and_lists_paths() -> None:
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    prompt = build_extract_prompt(["sections.cov.network_status"], turns)
    assert "sections.cov.network_status" in prompt
    assert "[0]" in prompt and "[1]" in prompt  # evidence_seq anchors


def test_parse_extract_response_maps_fields() -> None:
    data = [{"field_path": "p", "value": "in-network", "confidence": 90, "evidence_seq": 1}]
    out = parse_extract_response(data)
    assert out[0].field_path == "p" and out[0].evidence_seq == 1


def test_parse_extract_response_drops_blank_values() -> None:  # VR2-93
    data = [
        {"field_path": "a", "value": "", "confidence": 90, "evidence_seq": 1},
        {"field_path": "b", "value": "   ", "confidence": 90, "evidence_seq": 2},
        {"field_path": "c", "value": "No", "confidence": 90, "evidence_seq": 3},
    ]
    out = parse_extract_response(data)
    assert [e.field_path for e in out] == ["c"]


def test_build_judge_prompt_includes_extracted_and_transcript() -> None:
    extracted = [ExtractedField(field_path="a.b", value="yes", confidence=80, evidence_seq=0)]
    turns = [TranscriptTurn(0, "agent", "yes, covered")]
    prompt = build_judge_prompt(extracted, turns)
    assert "a.b" in prompt
    assert "[0]" in prompt


def test_parse_judge_response_maps_verdicts() -> None:
    data = [{"field_path": "a.b", "supported": True, "confidence": 95, "evidence": "yes, covered"}]
    out = parse_judge_response(data)
    assert out[0].field_path == "a.b"
    assert out[0].supported is True
    assert out[0].evidence == "yes, covered"


def test_build_judge_prompt_excludes_extractor_confidence() -> None:
    """Judge prompt must NOT leak extraction confidence—only value, field_path, evidence_seq.
    A distinctive confidence value (77) should NOT appear in the prompt."""
    extracted = [
        ExtractedField(
            field_path="sections.cov.network_status",
            value="in-network",
            confidence=77,  # Distinctive; must NOT appear in the prompt
            evidence_seq=1,
        )
    ]
    turns = [TranscriptTurn(0, "agent", "you are in network")]
    prompt = build_judge_prompt(extracted, turns)

    # The prompt MUST include the value and field_path and evidence_seq
    assert "in-network" in prompt
    assert "sections.cov.network_status" in prompt
    assert "evidence_seq" in prompt  # evidence_seq key must be present
    assert '"1"' in prompt or ": 1" in prompt  # evidence_seq value (1) must be present

    # The prompt MUST NOT include the extraction confidence (77)
    assert "77" not in prompt
    # confidence field must not appear in the extracted items JSON
    assert '"confidence": 77' not in prompt


async def test_judge_single_pass_when_all_fields_covered() -> None:
    """No re-judge when the first pass returns a verdict for every field."""
    stub = _StubJudgeClient([[_vd("a"), _vd("b")]])
    out = await stub.judge(extracted=[_ef("a"), _ef("b")], turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b"]
    assert len(stub.generate_calls) == 1


async def test_judge_rejudges_only_the_dropped_fields() -> None:
    """When Gemini drops part of a batch, judge re-runs on just the missing
    subset until every asked field has a verdict."""
    extracted = [_ef("a"), _ef("b"), _ef("c")]
    stub = _StubJudgeClient([[_vd("a")], [_vd("b"), _vd("c")]])
    out = await stub.judge(extracted=extracted, turns=[])
    assert sorted(v.field_path for v in out) == ["a", "b", "c"]
    assert len(stub.generate_calls) == 2


async def test_judge_enum_constrains_field_path_to_pending_paths() -> None:
    """Each pass constrains field_path to exactly the still-unjudged paths, so a
    verdict can never carry a reworded path — and the retry batch shrinks."""
    extracted = [_ef("a"), _ef("b"), _ef("c")]
    stub = _StubJudgeClient([[_vd("a")], [_vd("b"), _vd("c")]])
    await stub.judge(extracted=extracted, turns=[])
    assert _enum_of(stub.generate_calls[0]) == ["a", "b", "c"]
    assert _enum_of(stub.generate_calls[1]) == ["b", "c"]


async def test_judge_stops_after_max_attempts_when_a_field_never_returns() -> None:
    """A field the model never returns must not loop forever: bail after the cap
    and return the verdicts gathered so far."""
    stub = _StubJudgeClient([[_vd("a")]] * (_JUDGE_MAX_ATTEMPTS + 5))
    out = await stub.judge(extracted=[_ef("a"), _ef("b")], turns=[])
    assert [v.field_path for v in out] == ["a"]
    assert len(stub.generate_calls) == _JUDGE_MAX_ATTEMPTS


async def test_judge_returns_empty_without_calling_llm_when_nothing_extracted() -> None:
    stub = _StubJudgeClient([])
    out = await stub.judge(extracted=[], turns=[])
    assert out == []
    assert stub.generate_calls == []


def test_loads_response_parses_valid_json() -> None:
    """_loads_response returns parsed list for valid JSON text."""
    data = '[{"field_path": "a.b", "value": "yes", "confidence": 90, "evidence_seq": 0}]'
    result = _loads_response(data)
    assert result[0]["field_path"] == "a.b"


def test_loads_response_raises_on_none() -> None:
    """_loads_response raises RuntimeError on None (safety-blocked response)."""
    with pytest.raises(RuntimeError, match="empty LLM response"):
        _loads_response(None)


def test_loads_response_raises_on_empty_string() -> None:
    """_loads_response raises RuntimeError on empty string."""
    with pytest.raises(RuntimeError, match="empty LLM response"):
        _loads_response("")
