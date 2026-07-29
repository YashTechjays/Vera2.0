"""An evaluator LLM that grades a simulated call from its transcript.

The harness proves a call had the right SHAPE (the handoff fired, the tool was called). It cannot
say whether the call was any good: did VERA resolve the contradiction or merely repeat the question,
did she abandon a task mid-topic, did she wander off the compiled question list. This reads the
whole transcript and reports, dimension by dimension, what went right and wrong.

It grades the conversation as written, plus the two things a transcript cannot contain: the plan's
rules, and the task list with its questions. Without the rules, "were flow rules maintained?" is
unanswerable — they live in the compiled CallPlan, never in the conversation.

Every finding must cite a transcript line, and a finding citing a line that does not exist is
DISCARDED. The commonest failure of an LLM judge is a confident invented fault; making it point at
evidence turns that from a false alarm into a logged discard.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

DIMENSIONS: dict[str, str] = {
    "flow_rules": (
        "Did the call honour the plan's flow rules? When a rule's condition became true, did the "
        "call actually change course, or carry on as if nothing had happened?"
    ),
    "contradictions": (
        "When the representative contradicted themselves, did VERA notice it, push back once "
        "naming the conflict, and accept the correction? Missing a contradiction is a fail; "
        "pushing back repeatedly or arguing is also a fail."
    ),
    "task_handoffs": (
        "Did each task hand off at the right moment — every question answered or refused, nothing "
        "abandoned mid-topic? After a handoff, did VERA carry on rather than re-greeting, "
        "re-introducing herself, or re-asking something already answered?"
    ),
    "tool_calls": (
        "Was every tool call correct? task_complete only once the task was genuinely done; "
        "press_keypad only for digits the menu actually offered and never invented; end_call only "
        "at the very end; gap_complete only after the follow-up questions were re-asked."
    ),
    "ivr_navigation": (
        "Did VERA work the phone menu correctly, and hand off to the plan ONLY when a live human "
        "answered? Handing off to the automated assistant is a fail, however human it sounded."
    ),
    "question_coverage": (
        "Were the compiled questions actually asked, one at a time, with none skipped? Asking "
        "several at once, or moving on from an unanswered required question, is a fail."
    ),
    "scope_discipline": (
        "Did VERA stay on the compiled question list and invent no off-script questions?"
    ),
    "answer_handling": (
        "Were the representative's answers acknowledged, read back where a value needed "
        "confirming, and never silently re-asked later?"
    ),
    "gap_conduct": (
        "If a follow-up (gap) pass ran, did it read as a MID-CALL follow-up? Saying goodbye, "
        "thanking the rep as if finishing, or claiming everything was collected is a fail."
    ),
    "closing": (
        "Did the call collect the representative's name and a call reference number, and sign off "
        "exactly once? Signing off twice is a fail."
    ),
    "overall": (
        "Taking the call as a whole, would you put this in front of a real payer representative?"
    ),
}

_VERDICTS = ("pass", "fail", "n/a")


class _CompletionLLM(Protocol):
    async def complete(self, *, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class Finding:
    dimension: str
    verdict: str
    reason: str
    turn: int | None = None

    @property
    def failed(self) -> bool:
        return self.verdict == "fail"


@dataclass(frozen=True)
class Report:
    findings: list[Finding]
    discarded: int = 0

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.failed]

    def render(self, label: str) -> str:
        lines = [f"===== evaluation: {label} ====="]
        for finding in self.findings:
            mark = "FAIL" if finding.failed else finding.verdict
            cite = f"[{finding.turn}] " if finding.turn is not None else ""
            lines.append(f"  {finding.dimension:<18} {mark:<5} {cite}{finding.reason}")
        if self.discarded:
            lines.append(f"  ({self.discarded} discarded: cited a line not in the transcript)")
        return "\n".join(lines)


def _instructions() -> str:
    dims = "\n".join(f"- {name}: {question}" for name, question in DIMENSIONS.items())
    return (
        "You are grading a recorded phone call in which an automated insurance-verification "
        "assistant (VERA) called a health-plan representative (REP). You are given the numbered "
        "transcript, the call plan's rules, and the task list VERA was supposed to follow.\n\n"
        "Judge EXACTLY these dimensions, one verdict each:\n"
        f"{dims}\n\n"
        'Return ONLY a JSON array of {"dimension", "verdict", "reason", "turn"}. '
        'verdict is "pass", "fail", or "n/a" ("n/a" when the dimension did not arise — e.g. no '
        "contradiction occurred). reason is one short sentence. turn is the transcript line number "
        "your verdict is based on, or null if it genuinely applies to no single line.\n\n"
        "Rules for grading:\n"
        "- Base every judgement on the transcript. Never assume something happened that it does "
        "not show.\n"
        "- A turn number you cite MUST exist in the transcript. A finding citing a line that is "
        "not there will be thrown away.\n"
        "- Prefer 'n/a' over inventing a fault when a dimension did not come up.\n"
        "- No prose outside the JSON, and no code fence."
    )


def _brief(transcript: str, rules: str, tasks: str) -> str:
    return (
        f"# Call plan rules\n{rules or '(none)'}\n\n"
        f"# Tasks VERA was to follow, in order\n{tasks or '(none)'}\n\n"
        f"# Transcript\n{transcript}"
    )


def _parse(reply: str) -> list[Finding]:
    """Defensive parse — the prompt forbids a code fence, so tolerate one anyway rather than
    losing a whole evaluation to formatting."""
    text = reply.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension", "")).strip()
        verdict = str(item.get("verdict", "")).strip().lower()
        if dimension not in DIMENSIONS or verdict not in _VERDICTS:
            continue
        findings.append(
            Finding(
                dimension=dimension,
                verdict=verdict,
                reason=str(item.get("reason", "")).strip(),
                turn=_turn_of(item.get("turn")),
            )
        )
    return findings


def _turn_of(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def verify_citations(findings: list[Finding], line_count: int) -> tuple[list[Finding], int]:
    """Drop findings whose cited line is not in the transcript, and count the drops.

    A `turn` of None is allowed — some dimensions genuinely apply to the call as a whole — but an
    out-of-range number means the evaluator invented its evidence."""
    kept = [f for f in findings if f.turn is None or 0 <= f.turn < line_count]
    return kept, len(findings) - len(kept)


class CallEvaluator:
    """Grades one call. `llm` must be a `vera_core.llm.ResilientLLM`: this is an out-of-pipeline
    call, and vera_core/CLAUDE.md mandates that seam for every non-cascade LLM call."""

    def __init__(self, llm: _CompletionLLM) -> None:
        self._llm = llm

    async def evaluate(self, transcript: str, *, rules: str = "", tasks: str = "") -> Report:
        reply = await self._llm.complete(
            system=_instructions(), user=_brief(transcript, rules, tasks)
        )
        findings, discarded = verify_citations(_parse(reply), len(transcript.splitlines()))
        return Report(findings=findings, discarded=discarded)
