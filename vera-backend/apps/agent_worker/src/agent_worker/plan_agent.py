"""Plan-driven task agent: one generic agent per task in the compiled Call Plan.

It walks the task's fields with `forms.runtime.advance` (re-evaluating applicability after
each answer — the cascade), records answers into the shared `PlanRunState.answers`, and at
the task's end resolves the next task with `forms.runtime.next_task` and hands off to a
fresh agent (the LiveKit swap). No schema logic lives here — everything is driven by the
plan + the shared answer map.
"""

import logging

from livekit.agents import Agent, function_tool

from agent_worker.plan_prompt import build_plan_task_instructions
from agent_worker.plan_run_state import PlanRunState
from vera_core.forms.planning import PlanField, PlanTask
from vera_core.forms.runtime import advance, is_affirmation, next_task, normalize_answer

logger = logging.getLogger("agent_worker")


class PlanTaskAgent(Agent):
    def __init__(self, state: PlanRunState, task_key: str) -> None:
        self._state = state
        # Retained (unused today) for a future PHI seam re-add; see the removed PHIWallNodes.
        self._boundary = state.boundary
        self._session_id = state.session_id
        self._task: PlanTask = next(t for t in state.plan.tasks if t.task_key == task_key)
        # The field the agent is currently soliciting — answers are recorded against THIS,
        # never a freshly-inferred field, so a value can't land on the wrong path.
        self._asked: PlanField | None = None
        super().__init__(instructions=build_plan_task_instructions(state.plan, task_key))

    def pending_field(self) -> PlanField | None:
        """The next field to ask (auto-filling any now-inactive fields), or None when the
        task is exhausted. Re-evaluated against the live answer map on every call."""
        return advance(self._task, self._state.plan, self._state.answers)

    async def on_enter(self) -> None:
        if self._task.intro:
            self.session.say(self._task.intro)
        self._asked = self.pending_field()

    @function_tool(
        name="record_answer",
        description=(
            "Record the representative's answer to the question you just asked. Pass only "
            "the answer value (e.g. 'Yes', '$30', '20%'). Call this after every answer."
        ),
    )
    async def _record_answer(self, value: str) -> str | Agent:
        field = self._asked
        if field is None:
            # Nothing was being asked (task already exhausted) — don't silently drop the turn.
            logger.info(
                "record_answer with no field awaiting on task %s; ignoring", self._task.task_key
            )
            return self._complete()
        if (
            field.status == "CONFIRM"
            and field.prefilled_value is not None
            and is_affirmation(value)
        ):
            # A read-back the rep confirmed → keep the prefilled value; otherwise fall through
            # and treat the utterance as a correction (a fresh value to validate).
            recorded: str | None = field.prefilled_value
        else:
            recorded = normalize_answer(field, value)
        if recorded is None:
            # Couldn't validate against the field's expected/valid values — re-ask rather than
            # storing a value that would mis-gate the cascade. Keep `_asked` unchanged.
            logger.info("unrecognized answer for %s; re-prompting", field.field_path)
            return f"I didn't quite catch that — please ask again: {field.resolved_prompt}"
        self._state.answers[field.field_path] = recorded
        self._asked = self.pending_field()
        if self._asked is not None:
            return f"Recorded. Now ask the representative: {self._asked.resolved_prompt}"
        return self._complete()

    def _complete(self) -> str | Agent:
        """Run the outro, then hand off to the next task's agent — or end the call."""
        if self._task.outro:
            self.session.say(self._task.outro)
        target = next_task(self._task.task_key, self._state.plan, self._state.answers)
        if target is None:
            self.session.shutdown(drain=True)
            return "The verification is complete."
        logger.info("handoff: task %s -> %s", self._task.task_key, target)
        return PlanTaskAgent(self._state, target)
