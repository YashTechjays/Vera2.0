"""Rule engine: turns the live answer snapshot into at most one directive.

Pure and synchronous — the Observer calls `evaluate(answers)` after every answer it
records and passes whatever comes back to `controller.apply_directive_now(...)`, which
interrupts the session and swaps the agent from the Observer's own background task. This
module owns no I/O and never touches the session itself.

Two rule kinds, from the compiled CallPlan:
* `flow_rules` fire **once** — a terminate or a forward skip is a terminal redirect, so
  once a rule's `when` has held there is nothing to re-decide.

  **Fire-once counts one EVALUATION, not one successful apply** (accepted behavior, team
  decision).
* `contradictions` **re-arm**: they push back once per distinct set of values for their
  `fields`, so a rep who restates the same conflicting answer isn't re-challenged, but a
  genuinely new conflicting combination is.
* `numeric_consistencies` re-arm the same way, but their `when` is computed —
  the money-triplet checks in vera_core.forms.consistency — and their ReAsk
  reason embeds the actual recorded amounts.

Flow rules are evaluated before contradictions so a call that should end or jump wins over
a mere clarification when both would fire on the same turn.
"""

from collections.abc import Mapping
from typing import Any

from agent_worker.directives import Directive, ReAsk, SkipToTask, Terminate
from vera_core.forms.call_plan import CallPlan
from vera_core.forms.conditions import evaluate
from vera_core.forms.consistency import check_triplet, triplet_paths


class RuleEngine:
    def __init__(self, plan: CallPlan) -> None:
        self._flow_rules = plan.flow_rules
        self._contradictions = plan.contradictions
        self._numeric = plan.numeric_consistencies
        self._shared = plan.shared_conditions
        # Flow rules never re-fire; contradictions re-fire only when their fields'
        # values differ from the combination that last triggered them.
        self._fired_flow: set[str] = set()
        self._contradiction_snapshots: dict[str, tuple[Any, ...]] = {}
        self._numeric_snapshots: dict[str, tuple[Any, ...]] = {}

    def evaluate(self, answers: Mapping[str, Any]) -> Directive | None:
        for rule in self._flow_rules:
            if rule.rule_key in self._fired_flow:
                continue
            if evaluate(rule.when, answers, self._shared):
                self._fired_flow.add(rule.rule_key)
                if rule.skip_to_task is not None:
                    return SkipToTask(rule_key=rule.rule_key, task_key=rule.skip_to_task)
                return Terminate(rule_key=rule.rule_key)

        for contradiction in self._contradictions:
            snapshot = tuple(answers.get(field) for field in contradiction.fields)
            if snapshot == self._contradiction_snapshots.get(contradiction.rule_key):
                continue  # same conflicting values we already pushed back on
            if evaluate(contradiction.when, answers, self._shared):
                self._contradiction_snapshots[contradiction.rule_key] = snapshot
                return ReAsk(
                    rule_key=contradiction.rule_key,
                    reason=contradiction.reason,
                    clarify=contradiction.clarify,
                )

        for consistency in self._numeric:
            snapshot = tuple(answers.get(path) for path in triplet_paths(consistency.triplet))
            if snapshot == self._numeric_snapshots.get(consistency.rule_key):
                continue  # same impossible values we already pushed back on
            reason = check_triplet(consistency.triplet, answers)
            if reason is not None:
                self._numeric_snapshots[consistency.rule_key] = snapshot
                return ReAsk(
                    rule_key=consistency.rule_key,
                    reason=reason,
                    clarify=consistency.clarify,
                )
        return None
