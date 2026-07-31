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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from vera_core.forms.call_plan import CallPlan
from vera_core.forms.conditions import is_applicable

DIMENSIONS: dict[str, str] = {
    "flow_rules": (
        "Compare the recorded facts with the transcript. If a flow rule's condition became true in "
        "the conversation but the facts show NO rule fired, that is a FAIL. If one did fire, did "
        "the call then do what that rule's stated intent requires? Never infer firing from the "
        "conversation changing course — the fact block is authoritative."
    ),
    "contradictions": (
        "If the representative contradicted themselves in a way one of the listed contradiction "
        "rules covers, but the facts show it never fired, that is a FAIL. If it did fire, did VERA "
        "push back once naming the conflict — ideally close to the rule's expected wording — and "
        "then accept the correction? Pushing back repeatedly or arguing is also a FAIL."
    ),
    "task_handoffs": (
        "Did each task hand off at the right moment — every APPLICABLE question answered or "
        "refused, nothing abandoned mid-topic? A task whose questions are all gated out should "
        "be completed immediately; that is not abandonment. After a handoff, did VERA carry on "
        "rather than re-greeting, re-introducing herself, or re-asking something answered?"
    ),
    "tool_calls": (
        "Was every tool call correct? task_complete only once the task was genuinely done; "
        "press_keypad only for digits the menu actually offered and never invented; end_call only "
        "at the very end; gap_complete only after the follow-up questions were re-asked. "
        "A TOOL line carries VERA's own stated reason in parentheses. Judge the reason too: "
        "one that the transcript contradicts, or that does not justify the call it was given "
        "for, is a FAIL even when the call itself looks defensible. A TOOL line with no reason "
        "is a defect in the harness, not in the call — ignore it."
    ),
    "ivr_navigation": (
        "Did VERA work the phone menu correctly, and hand off to the plan ONLY when a live human "
        "answered? Handing off to the automated assistant is a fail, however human it sounded."
    ),
    "question_coverage": (
        "Were the APPLICABLE compiled questions asked, one at a time, with none skipped? Questions "
        "marked as gated out do not count — omitting them is correct. Asking several at once, or "
        "moving on from an unanswered applicable question, is a fail."
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
        "- The 'Recorded facts' block is GROUND TRUTH from instrumentation, not inference. Whether "
        "a rule fired is INVISIBLE in a transcript, so never conclude one fired because the call "
        "changed course or ended early. If the facts say no rule fired while the plan says one "
        "should have, that is a FAIL — not a pass.\n"
        f"- A question marked {GATED_OUT} is excluded by the call plan's gates. VERA is CORRECT "
        "not to ask it, and asking it anyway is the fault. Never count a gated-out question as "
        "missing coverage, and never treat a task whose questions are all gated out as skipped.\n"
        "- Base every judgement on the transcript. Never assume something happened that it does "
        "not show.\n"
        "- A turn number you cite MUST exist in the transcript. A finding citing a line that is "
        "not there will be thrown away.\n"
        "- Prefer 'n/a' over inventing a fault when a dimension did not come up.\n"
        "- No prose outside the JSON, and no code fence."
    )


GATED_OUT = "[GATED OUT — must NOT be asked on this call]"


def render_rules(plan: CallPlan) -> str:
    """The plan's rules as the judge sees them. A transcript cannot contain these, so without them
    "were flow rules maintained?" is unanswerable."""
    lines: list[str] = []
    for rule in plan.flow_rules:
        action = (
            f"skip ahead to task `{rule.skip_to_task}`" if rule.skip_to_task else "end the call"
        )
        lines.append(f"- flow rule `{rule.rule_key}`: when {rule.when} -> VERA must {action}")
        if rule.note:
            lines.append(f"    intent: {rule.note}")
    for bad in plan.contradictions:
        lines.append(
            f"- contradiction `{bad.rule_key}`: when {bad.when} -> VERA must push back once, "
            "then accept the correction"
        )
        lines.append(f"    why: {bad.reason}")
        if bad.clarify:
            lines.append(f"    expected wording: {bad.clarify}")
    return "\n".join(lines)


def render_tasks(plan: CallPlan, answers: Mapping[str, Any] | None = None) -> str:
    """The compiled task list, with each question marked when its GATES exclude it from this call.

    Without this the judge treats a correct gate-driven skip as a coverage failure — it has no way
    to know gates exist. Applicability is evaluated against the call's final answer snapshot, the
    same input `gap_fields()` uses."""
    values: Mapping[str, Any] = answers if answers is not None else plan.prefilled
    lines: list[str] = []
    for index, task in enumerate(plan.tasks):
        rendered: list[str] = []
        gated = 0
        for field in task.fields:
            if is_applicable(field.gates, values, plan.shared_conditions):
                rendered.append(f"     - {field.title}")
            else:
                gated += 1
                rendered.append(f"     - {field.title} {GATED_OUT}")
        header = f"{index + 1}. `{task.task_key}` ({task.title})"
        if task.fields and gated == len(task.fields):
            header += "  <- EVERY question is gated out; completing this task without asking "
            header += "anything is CORRECT"
        lines.append(header)
        lines.extend(rendered)
    return "\n".join(lines)


def render_facts(
    fired_rules: Sequence[str], answers_extracted: int, *, focused: bool = False
) -> str:
    """Recorded facts the TRANSCRIPT CANNOT SHOW, so the judge has to be told them.

    A directive leaves no trace in the conversation. A judge reading only the transcript therefore
    infers "the rule fired" from the call ending early — and passes a rule that never ran. This
    block is the authoritative record, taken from instrumentation."""
    lines = []
    if fired_rules:
        joined = ", ".join(f"`{rule}`" for rule in fired_rules)
        lines.append(f"- Rules that ACTUALLY fired (authoritative): {joined}")
    else:
        lines.append("- NO rule fired on this call (authoritative).")
    lines.append(
        f"- The Observer extracted {answers_extracted} answer(s). A rule can only fire from an "
        "extracted answer, so with 0 no rule could have fired, whatever the conversation suggests."
    )
    if focused:
        lines.append(
            "- This plan was NARROWED to a subset of fields. Known defect: narrowing does not "
            "shorten the spoken question list, so VERA still asks the task's full set. Questions "
            "outside the task list below are therefore NOT off-script — return `n/a` for "
            "scope_discipline on this call."
        )
    return "\n".join(lines)


def _brief(transcript: str, rules: str, tasks: str, facts: str) -> str:
    return (
        f"# Recorded facts about this run (ground truth)\n{facts or '(none)'}\n\n"
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

    async def evaluate(
        self, transcript: str, *, rules: str = "", tasks: str = "", facts: str = ""
    ) -> Report:
        reply = await self._llm.complete(
            system=_instructions(), user=_brief(transcript, rules, tasks, facts)
        )
        findings, discarded = verify_citations(_parse(reply), len(transcript.splitlines()))
        return Report(findings=findings, discarded=discarded)
