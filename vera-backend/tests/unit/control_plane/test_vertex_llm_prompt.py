import pytest

from control_plane.llm import (
    VertexLLMClient,
    build_extract_prompt,
    build_judge_prompt,
    parse_extract_response,
    parse_judge_response,
)
from vera_core.integrations.llm import ExtractedField, TranscriptTurn

_loads_response = VertexLLMClient._loads_response


def test_extract_prompt_numbers_turns_and_lists_paths() -> None:
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    prompt = build_extract_prompt(["sections.cov.network_status"], turns)
    assert "sections.cov.network_status" in prompt
    assert "[0]" in prompt and "[1]" in prompt  # evidence_seq anchors


def test_parse_extract_response_maps_fields() -> None:
    data = [{"field_path": "p", "value": "in-network", "confidence": 90, "evidence_seq": 1}]
    out = parse_extract_response(data)
    assert out[0].field_path == "p" and out[0].evidence_seq == 1


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
