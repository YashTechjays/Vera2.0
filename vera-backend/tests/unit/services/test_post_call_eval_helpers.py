from vera_core.integrations.llm import TranscriptTurn
from vera_core.services.post_call_eval import evidence_text, has_phi_token


def test_has_phi_token_detects_bracket_token() -> None:
    assert has_phi_token("[[MEMBER_ID_1]]") is True
    assert has_phi_token("in-network") is False


def test_evidence_text_safe_index() -> None:
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    assert evidence_text(turns, 1) == "in network"
    assert evidence_text(turns, 9) is None
