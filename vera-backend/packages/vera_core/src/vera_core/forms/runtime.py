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

from collections.abc import Mapping, MutableMapping

from vera_core.forms.conditions import (
    Applicability,
    evaluate,
    resolve_applicability,
)
from vera_core.forms.planning import CallPlan, PlanField, PlanTask

Answers = Mapping[str, str]


def advance(task: PlanTask, plan: CallPlan, answers: MutableMapping[str, str]) -> PlanField | None:
    """The next field to ask in `task`, or None when the task is exhausted. Out-of-scope
    fields are auto-filled with their `inapplicable_value` and never asked; fields whose
    gates aren't decidable yet are skipped this pass (a later answer may reach them)."""
    shared = plan.shared_conditions or {}
    for field in task.fields:
        if field.field_path in answers:
            continue
        if field.status != "COLLECT":
            # confirm/context prefill is deferred (PHI vault); skip for now.
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


def next_task(current_task_key: str, plan: CallPlan, answers: Answers) -> str | None:
    """The next task to hand off to, or None to end the call. A `terminate`/`skip_to_task`
    flow rule jumps ahead; otherwise advance to the next reachable task. The final task
    always runs (it captures the rep's name + call reference before hangup)."""
    shared = plan.shared_conditions or {}
    order = [t.task_key for t in plan.tasks]
    idx = order.index(current_task_key)

    for rule in plan.flow_rules or []:
        if (
            rule.skip_to_task
            and evaluate(rule.when, answers, shared)
            and order.index(rule.skip_to_task) > idx
        ):
            return rule.skip_to_task

    last = order[-1]
    for task in plan.tasks[idx + 1 :]:
        if task.task_key == last or _reachable(task, plan, answers):
            return task.task_key
    return None
