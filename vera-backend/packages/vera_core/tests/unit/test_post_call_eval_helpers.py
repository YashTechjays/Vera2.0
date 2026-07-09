from vera_core.integrations.llm import ExtractedField, JudgeVerdict, TranscriptTurn
from vera_core.services.post_call_eval import (
    evidence_text,
    has_phi_token,
    needs_review,
)

FLOOR = 60


def test_has_phi_token_detects_bracket_token():
    assert has_phi_token("[[MEMBER_ID_1]]") is True
    assert has_phi_token("in-network") is False


def test_needs_review_when_value_still_tokenized():
    ef = ExtractedField("p", "[[MEMBER_ID_1]]", 95, 0)
    v = JudgeVerdict("p", True, 95, "e")
    assert needs_review(ef, v, floor=FLOOR) is True


def test_needs_review_when_unsupported_or_low_confidence():
    ef = ExtractedField("p", "in-network", 95, 0)
    assert needs_review(ef, JudgeVerdict("p", False, 95, "e"), floor=FLOOR) is True
    assert needs_review(ef, JudgeVerdict("p", True, 40, "e"), floor=FLOOR) is True
    assert needs_review(ef, None, floor=FLOOR) is True


def test_no_review_when_supported_and_confident_and_clean():
    ef = ExtractedField("p", "in-network", 95, 0)
    assert needs_review(ef, JudgeVerdict("p", True, 80, "e"), floor=FLOOR) is False


def test_evidence_text_safe_index():
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    assert evidence_text(turns, 1) == "in network"
    assert evidence_text(turns, 9) is None
