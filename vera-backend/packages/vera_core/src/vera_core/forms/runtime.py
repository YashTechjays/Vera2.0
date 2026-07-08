"""Phase 2 — plan execution core: the field walk and the handoff-target resolver.

Pure and DB-free. Everything is driven by a `CallPlan` plus the shared answer map
(root-anchored path → raw value); there is zero schema logic here, and no LiveKit. The
worker's task agents are thin shells over these two functions:

* :func:`advance` — the intra-task cascade. Right before each field it re-resolves
  applicability against the *current* answers (spec Seam 1), silently writing the
  `inapplicable_value` for out-of-scope fields and returning the next field to ask.
* :func:`next_task` — the inter-task router. Honors `flow_rules` (`skip_to_task`),
  task-level and all-inactive skipping, and the always-run final task.
"""

import re
from collections.abc import Mapping, MutableMapping

from vera_core.forms.conditions import (
    Applicability,
    evaluate,
    resolve_applicability,
)
from vera_core.forms.dsl import FlowRule
from vera_core.forms.planning import CallPlan, PlanField, PlanTask

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _standalone(needle: str, haystack: str) -> bool:
    """`needle` appears as a standalone token in `haystack` (both lowercased) — tolerant of
    surrounding filler ("yes it's covered" → "yes") but not of embedding ("no" ⊄ "nope")."""
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def normalize_answer(field: PlanField, raw: str) -> str | None:
    """Map a raw spoken answer to the field's canonical value, or None when it can't be
    validated (the agent then re-prompts instead of storing a value that mis-gates the
    cascade). Enum answers must resolve to a listed value; numeric answers must parse and
    fall in range; free text passes through trimmed."""
    text = raw.strip()
    if not text:
        return None
    lowered = text.lower()
    candidates = (field.expected_values or []) + (field.special_values or [])
    # Prefer an exact (case-insensitive) match on any candidate, then fall back to a
    # standalone-token match — so "yes" beats a stray "no" embedded in filler.
    for candidate in candidates:
        if lowered == candidate.lower():
            return candidate
    for candidate in candidates:
        if _standalone(candidate.lower(), lowered):
            return candidate
    if field.expected_values:
        return None  # an enum must resolve to one of its listed values
    validation = field.validation
    if validation is not None and validation.range is not None:
        match = _NUMBER_RE.search(text.replace(",", ""))
        if match is None:
            return None
        number = float(match.group())
        rng = validation.range
        if (rng.min is not None and number < rng.min) or (rng.max is not None and number > rng.max):
            return None
    return text


Answers = Mapping[str, str]

_AFFIRMATIONS = frozenset(
    {"yes", "yeah", "yep", "yup", "correct", "right", "confirmed", "confirm", "sure", "affirmative"}
)


def is_affirmation(text: str) -> bool:
    """True if `text` reads as a plain 'yes, that's correct' — used to accept a confirm
    read-back. A correction (a different value) is not an affirmation."""
    lowered = text.lower()
    return any(_standalone(word, lowered) for word in _AFFIRMATIONS)


def advance(task: PlanTask, plan: CallPlan, answers: MutableMapping[str, str]) -> PlanField | None:
    """The next field to ask in `task`, or None when the task is exhausted. Out-of-scope
    fields are auto-filled with their `inapplicable_value` and never asked; fields whose
    gates aren't decidable yet are skipped this pass (a later answer may reach them).
    COLLECT fields are asked; CONFIRM fields are read back; KNOWN/PENDING are never spoken."""
    shared = plan.shared_conditions or {}
    for field in task.fields:
        if field.field_path in answers:
            continue
        if field.status not in ("COLLECT", "CONFIRM"):
            # context (KNOWN) and deferred PENDING_CONTEXT fields are never spoken.
            continue
        match resolve_applicability(tuple(field.applicable_when), answers, shared):
            case Applicability.APPLICABLE:
                return field
            case Applicability.INACTIVE:
                if field.inapplicable_value is not None:
                    answers[field.field_path] = field.inapplicable_value
            case Applicability.NOT_REACHABLE_YET:
                continue
    return None


def _has_askable(task: PlanTask, plan: CallPlan, answers: Answers) -> bool:
    """Would the cascade actually ask something? Replays the walk on a throwaway copy,
    letting inactive fills unblock deeper fields, until a question surfaces or the walk
    goes dry. Reusing `advance` keeps one source of truth for traversal."""
    probe = dict(answers)
    while True:
        before = len(probe)
        if advance(task, plan, probe) is not None:
            return True
        if len(probe) == before:  # nothing asked and nothing filled → dry
            return False


def _reachable(task: PlanTask, plan: CallPlan, answers: Answers) -> bool:
    shared = plan.shared_conditions or {}
    if task.applicable_when is not None and not evaluate(task.applicable_when, answers, shared):
        return False
    return _has_askable(task, plan, answers)


def _fired_terminate_rule(plan: CallPlan, answers: Answers) -> FlowRule | None:
    """The first `terminate_call` flow rule whose condition holds, or None — the single
    source of truth for both the mid-task check and `next_task`'s routing."""
    shared = plan.shared_conditions or {}
    for rule in plan.flow_rules or []:
        if rule.action == "terminate_call" and evaluate(rule.when, answers, shared):
            return rule
    return None


def terminate_fired(plan: CallPlan, answers: Answers) -> bool:
    """True once a `terminate_call` flow rule's condition holds — checked after every answer
    so a disqualifying reply (e.g. no out-of-network coverage) ends the interview mid-task
    instead of only at the current task's boundary."""
    return _fired_terminate_rule(plan, answers) is not None


def next_task(current_task_key: str, plan: CallPlan, answers: Answers) -> str | None:
    """The next task to hand off to, or None to end the call.

    A fired `terminate_call` flow rule ends the interview: it jumps to its `skip_to_task`
    when set and ahead, otherwise to the FINAL task (so the always-run wrap-up still captures
    the rep's name + call reference), or ends outright if already there. Absent a rule, we
    advance to the next reachable task; the final task always runs."""
    order = [t.task_key for t in plan.tasks]
    idx = order.index(current_task_key)
    last = order[-1]

    rule = _fired_terminate_rule(plan, answers)
    if rule is not None:
        # skip_to_task is optional; a pure terminate rule falls back to the final task so the
        # interview still ends at wrap-up instead of interrogating a dead-end verification.
        target = rule.skip_to_task or last
        return target if order.index(target) > idx else None

    for task in plan.tasks[idx + 1 :]:
        if task.task_key == last or _reachable(task, plan, answers):
            return task.task_key
    return None
