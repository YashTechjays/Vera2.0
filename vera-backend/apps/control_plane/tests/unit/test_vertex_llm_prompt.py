from control_plane.llm import (
    build_extract_prompt,
    build_judge_prompt,
    parse_extract_response,
    parse_judge_response,
)
from vera_core.integrations.llm import ExtractedField, TranscriptTurn


def test_extract_prompt_numbers_turns_and_lists_paths():
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    prompt = build_extract_prompt(["sections.cov.network_status"], turns)
    assert "sections.cov.network_status" in prompt
    assert "[0]" in prompt and "[1]" in prompt  # evidence_seq anchors


def test_parse_extract_response_maps_fields():
    data = [{"field_path": "p", "value": "in-network", "confidence": 90, "evidence_seq": 1}]
    out = parse_extract_response(data)
    assert out[0].field_path == "p" and out[0].evidence_seq == 1


def test_build_judge_prompt_includes_extracted_and_transcript():
    extracted = [ExtractedField(field_path="a.b", value="yes", confidence=80, evidence_seq=0)]
    turns = [TranscriptTurn(0, "agent", "yes, covered")]
    prompt = build_judge_prompt(extracted, turns)
    assert "a.b" in prompt
    assert "[0]" in prompt


def test_parse_judge_response_maps_verdicts():
    data = [
        {"field_path": "a.b", "supported": True, "confidence": 95, "evidence": "yes, covered"}
    ]
    out = parse_judge_response(data)
    assert out[0].field_path == "a.b"
    assert out[0].supported is True
    assert out[0].evidence == "yes, covered"
