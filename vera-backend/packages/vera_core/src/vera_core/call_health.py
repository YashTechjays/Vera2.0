"""Call-health analyzer contract — shared vocabulary between the agent worker's
observer (which runs the LLM analysis) and the control plane's consumer (which
persists the result).

Prompt-cache rules (spec §4.2): the system prompt is BYTE-IDENTICAL for every
analysis of every call, the transcript renders append-only (a turn, once
rendered, never changes), and anything dynamic goes AFTER the transcript — so
successive analyses of one call share a growing identical prefix that Vertex
Gemini / OpenAI implicit prompt caching discounts. The window is bounded by
chunked re-anchoring, not per-request sliding, for the same reason.

PHI: transcript text and the LLM's `reason` are PHI — nothing here logs content
(parse failures log exception type names only).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel

from vera_core.models.enums import CallHealthFlag, values_of
from vera_core.transcript import ROLE_DTMF, TurnRole, TurnSource

logger = logging.getLogger(__name__)

HEALTH_SYSTEM_PROMPT = """\
Given the ongoing conversation transcription so far, analyse whether the call
can be completed fully by the bot agent. Give a call health score from 0 to 100
and categorize whether a supervisor intervention is needed or the bot can
continue and finish the call itself.

Rules:
- Early automated IVR/phone-menu navigation is normal and must never be flagged
  as a conversation loop.
- If the conversation so far is insufficient to judge, return
  {"assessable": false} - never guess a low score to express uncertainty.
- A low score must always mean the call is going badly, never "unsure".
- Do not converse. Respond with ONLY a JSON object, no markdown fences, in
  exactly one of these shapes:
  {"assessable": false}
  {"assessable": true, "call_health_score": 78, "intervention_flag": "<flag>",
   "reason": "<one short sentence>"}
- intervention_flag must be one of: none, supervisor_requested,
  repeated_questions, hallucination, conversation_loop, long_silence,
  off_script, low_confidence, other."""

# Appended AFTER the transcript in the user message — the dynamic tail must never
# sit ahead of the cacheable transcript prefix.
HEALTH_USER_SUFFIX = "\n\nAssess the call health now. Respond with ONLY the JSON object."

_SPEAKER_LABELS = {"rep": "Payer rep", "bot": "Vera (agent)", "supervisor": "Supervisor"}
# On overflow, truncate once to this many newest turns, then grow back to the cap:
# the prefix stays byte-identical BETWEEN re-anchors (~1 cache miss per 20 turns).
_REANCHOR_KEEP = 40
_MAX_REASON_LEN = 500

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_VALID_FLAGS = frozenset(values_of(CallHealthFlag))


@dataclass(frozen=True)
class HealthResult:
    """One normalized assessable analysis."""

    score: int  # clamped to 0-100
    flag: str  # a CallHealthFlag value
    reason: str  # PHI — never log


class _RawAssessment(BaseModel):
    """The LLM's JSON contract, before normalization."""

    assessable: bool = True
    call_health_score: float | None = None
    intervention_flag: str | None = None
    reason: str | None = None


def parse_assessment(text: str) -> HealthResult | None:
    """LLM reply -> normalized result. None means "no result this cycle" — the
    model said `assessable: false`, omitted the score, or ignored the contract;
    all three are a complete no-op for the observer (a low score must always
    mean "going badly", never "could not parse"). Unknown flags coerce to
    `other`; a missing flag reads as `none`."""
    raw = text.strip()
    fenced = _JSON_FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        parsed = _RawAssessment.model_validate_json(raw)
    except Exception as exc:  # the reply is PHI — type name only
        logger.warning("health assessment parse failed: %s", type(exc).__name__)
        return None
    if not parsed.assessable or parsed.call_health_score is None:
        return None
    flag = (parsed.intervention_flag or CallHealthFlag.NONE.value).strip().lower()
    if flag not in _VALID_FLAGS:
        flag = CallHealthFlag.OTHER.value
    score = max(0, min(100, round(parsed.call_health_score)))
    return HealthResult(score=score, flag=flag, reason=(parsed.reason or "")[:_MAX_REASON_LEN])


class HealthTranscript:
    """Bounded, prefix-stable transcript window (see module docstring)."""

    def __init__(self, *, max_turns: int = 60) -> None:
        if max_turns <= _REANCHOR_KEEP:
            raise ValueError(f"max_turns must exceed {_REANCHOR_KEEP}")
        self._max_turns = max_turns
        self._lines: list[str] = []

    @property
    def turn_count(self) -> int:
        return len(self._lines)

    def add(self, role: TurnRole, source: TurnSource, text: str) -> None:
        label = _SPEAKER_LABELS.get(source, source)
        if role == ROLE_DTMF:
            label = f"{label} [keypad]"
        self._lines.append(f"{label}: {text}")
        if len(self._lines) > self._max_turns:
            del self._lines[: len(self._lines) - _REANCHOR_KEEP]

    def render_user_message(self) -> str:
        return "\n".join(self._lines) + HEALTH_USER_SUFFIX
