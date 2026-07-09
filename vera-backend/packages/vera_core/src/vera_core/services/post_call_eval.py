"""Post-call re-read: extract collected fields from the (de-identified) transcript,
persist them, judge each, and decide the form's terminal status. Pure helpers here;
the DB orchestration (evaluate_call) is added in a later task.
"""

from phi_codec.tokens.token import TOKEN_RE

from vera_core.integrations.llm import ExtractedField, JudgeVerdict, TranscriptTurn

# A judge verdict below this confidence (or unsupported) routes the field to review.
REVIEW_CONFIDENCE_FLOOR = 60

PHI_TOKEN_RE = TOKEN_RE


def has_phi_token(value: str) -> bool:
    """True if the extracted value still contains a `[[TYPE_N]]` PHI token — meaning the
    LLM surfaced an identifier we cannot safely materialize (no live vault). Such fields
    are routed to review rather than stored as a token."""
    return PHI_TOKEN_RE.search(value) is not None


def needs_review(extracted: ExtractedField, verdict: JudgeVerdict | None, *, floor: int) -> bool:
    if has_phi_token(extracted.value):
        return True
    if verdict is None or not verdict.supported:
        return True
    return verdict.confidence < floor


def evidence_text(turns: list[TranscriptTurn], evidence_seq: int) -> str | None:
    if 0 <= evidence_seq < len(turns):
        return turns[evidence_seq].text
    return None
