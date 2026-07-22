"""Plan runtime: one PlanTaskAgent per CallPlan task, chained by tool handoff.

The control plane compiles a CallPlan at dispatch (schema structure + prompt
wording, `vera_core.forms.call_plan`); the worker loads it at call start and
builds this runtime: every task agent is constructed BEFORE the call starts (a
broken plan fails the whole build up front, never mid-call), tasks run
sequentially via the tool-returns-Agent handoff (the same mechanism as the IVR
navigator's `transfer_to_verification`), and the chain ends in a WrapUpAgent
that closes the call.

Division of labor (two writers, disjoint state):
* The conversational agents are DIALOGUE-ONLY — no field paths, no answer
  tools. Their single shared-state write is the `active_task_id` cursor, on
  task entry, fire-and-forget (a Redis blip must never delay speech).
* Answers are owned by the Phase-2 Observer, which watches the cursor and is
  the sole writer of `PlanRunState.answers`.

`PlanRunController.apply_directive_now` is the Phase-2 seam where the rule
engine redirects the live conversation (terminate → wrap-up / skip ahead /
re-ask): it interrupts the bot then swaps the agent or re-asks, serialized on
the controller lock against an in-flight `task_complete` handoff so the two can
never double-swap the active agent.
"""

import asyncio
import logging
from typing import Any

from livekit.agents import Agent, AgentSession, llm

from agent_worker.agent import VeraAgent
from agent_worker.directives import Directive, ReAsk, SkipToTask, Terminate
from agent_worker.handoff import carry_chat_ctx
from agent_worker.intervention import TakeoverState, takeover_engaged
from agent_worker.prompt import CARTESIA_MARKUP_GUIDE, SCOPE_DISCIPLINE
from vera_core.forms.call_plan import CallPlan
from vera_core.forms.conditions import evaluate
from vera_core.plan_store import PlanRunStateService

logger = logging.getLogger("agent_worker")

# Cursor value while the wrap-up agent holds the call. The leading "@" can never
# appear in an authored task_key (dsl.KEY_RE is ^[a-z][a-z0-9_]*$), so the
# sentinel is collision-proof by construction, not by author discipline.
WRAP_UP_TASK_KEY = "@wrap_up"

_WRAP_UP_DIRECTIVE = (
    "Every task on the call plan is complete. Thank the representative for their "
    "time, say a brief goodbye, then call end_call."
)

# Spoken after a task's intro to make the bot proactively lead into the task
_OPENING_DIRECTIVE = "Continue the call now by asking your next unanswered question."


def _instructions(plan: CallPlan, task_block: str, *, extra_instructions: str | None) -> str:
    """Session block (+ the form's Known-information prefill, + the tenant's
    persona-tweak extra instructions, when present) + one task-specific block +
    the scope-discipline guardrail + the Cartesia TTS markup guide — fused once, at
    build time. The scope guardrail keeps the LLM on the compiled question list (no
    invented off-script questions); the markup guide keeps CPT codes `<spell>`-wrapped
    (the compiled prompts carry no TTS guidance)."""
    parts = [
        f"# Persona\n{plan.session.persona}",
        f"# Goal\n{plan.session.goal}",
        f"# Ground rules\n{plan.session.base_instructions}",
    ]
    if plan.known_information:
        parts.append(f"# Known information\n{plan.known_information}")
    if plan.on_file_values:
        # Confirm-role prefills the agent should READ BACK to confirm (not ask openly),
        # so a rep correction surfaces as an explicit mismatch instead of a silent swap.
        parts.append(
            "# Values already on file (confirm these; do not ask for them open-endedly)\n"
            f"{plan.on_file_values}"
        )
    if extra_instructions:
        parts.append(f"# Additional instructions\n{extra_instructions}")
    parts.append(task_block)
    parts.append(SCOPE_DISCIPLINE)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)


class PlanTaskAgent(Agent):
    """One schema task's conversation. Dialogue-only by construction: its sole
    tool is `task_complete`, and its sole shared-state write is the cursor."""

    def __init__(self, controller: "PlanRunController", task_index: int) -> None:
        self._controller = controller
        self._task_index = task_index
        self._task = controller.plan.tasks[task_index]
        super().__init__(
            instructions=_instructions(
                controller.plan,
                f"# Current task: {self._task.title}\n{self._task.prompt}",
                extra_instructions=controller.extra_instructions,
            ),
        )

    async def on_enter(self) -> None:
        self._controller.note_task_entered(self._task_index)
        opening = self._controller.opening_line(self._task.intro)
        if opening:
            self.session.say(opening)
            self.session.generate_reply(instructions=_OPENING_DIRECTIVE)

    @llm.function_tool(
        name="task_complete",
        description=(
            "Move on to the next task of the call. Call this ONLY when every "
            "question in the current task has been answered or the representative "
            "has confirmed they cannot answer what remains. Never call it to skip "
            "questions that are still answerable."
        ),
    )
    async def _task_complete(self) -> Agent | str:
        if takeover_engaged(self.session):
            # A str is a tool result, so the plan parks here. Returning `self` would
            # re-fire on_enter and speak the intro again.
            return "A human supervisor has taken over this call. Stay silent."
        if self._task.outro:
            # Exit speech first; LiveKit drains queued speech through the swap.
            self.session.say(self._task.outro)
        successor = await self._controller.advance_from(self._task_index)
        # Carry the call so far into the successor — LiveKit doesn't for a
        # tool-returned agent, so without this it re-greets and re-asks.
        await carry_chat_ctx(self, successor)
        return successor


class WrapUpAgent(VeraAgent):
    """Terminal agent: thanks the rep and ends the call (end_call is inherited
    from VeraAgent); entered from the last task or (Phase 2) a terminate
    directive. Overrides on_enter — it closes, it never re-greets."""

    def __init__(self, controller: "PlanRunController") -> None:
        self._controller = controller
        super().__init__(
            instructions=_instructions(
                controller.plan,
                "# Current task: Wrap up\n"
                "The verification is complete. Close the call politely and briefly; "
                "do not open new topics or re-ask anything.",
                extra_instructions=controller.extra_instructions,
            ),
        )

    async def on_enter(self) -> None:
        self._controller.note_wrap_up_entered()  # the cursor still records where we parked
        if takeover_engaged(self.session):
            logger.info("wrap-up entered under supervisor takeover; skipping the goodbye")
            return
        self.session.generate_reply(instructions=_WRAP_UP_DIRECTIVE)


class PlanRunController:
    """Owns the pre-built agent chain and the run's shared-state seam.

    All agents are constructed in ``__init__`` — before ``session.start`` — so a
    malformed plan fails the build (and the worker fails fast, hanging up) instead
    of exploding mid-conversation.
    """

    def __init__(
        self,
        plan: CallPlan,
        *,
        room_name: str,
        run_state: PlanRunStateService,
        greeting: str | None = None,
        extra_instructions: str | None = None,
    ) -> None:
        if not plan.tasks:
            raise ValueError("call plan has no tasks")
        self.plan = plan
        self.room_name = room_name
        self.greeting = greeting
        # Tenant persona-tweak overlay, appended to every plan agent's instructions.
        self.extra_instructions = extra_instructions
        self._opened = False  # has any task agent spoken the call's opening line yet
        self._run_state = run_state
        # In-process answers snapshot for applicability/skip decisions, seeded
        # with the form's intake prefill (so gates work from call start); the
        # Phase-2 Observer keeps it current. Redis stays the cross-process truth.
        self._answers: dict[str, Any] = dict(plan.prefilled)
        self.active_task_index: int | None = None
        # Serializes handoffs against rule-engine directives so a directive and an
        # in-flight `task_complete` handoff can never double-swap the active agent.
        self.lock = asyncio.Lock()
        # Counts task advances; a takeover interlock reads it to assert the plan did
        # NOT advance while a supervisor was in control (test_takeover_interlock).
        self.generation = 0
        # The live AgentSession, attached after session.start (see attach_session).
        # apply_directive_now drives it to interrupt/swap the bot on a rule fire.
        self._session: AgentSession[TakeoverState] | None = None
        # Fire-and-forget cursor writes: strong refs (a bare create_task result
        # can be GC'd mid-flight), drained in tests via drain_cursor_writes.
        self._cursor_writes: set[asyncio.Task[None]] = set()

        self.agents = [PlanTaskAgent(self, i) for i in range(len(plan.tasks))]
        self.wrap_up_agent = WrapUpAgent(self)

    # -- conversation-path API ------------------------------------------------

    def first_agent(self) -> Agent:
        return self._agent_at(self._next_applicable(0))

    async def advance_from(self, index: int) -> Agent:
        async with self.lock:
            self.generation += 1
            return self._agent_at(self._next_applicable(index + 1))

    def opening_line(self, intro: str | None) -> str | None:
        """What the entering task agent speaks. An explicit tenant greeting
        replaces the intro of whichever task agent actually opens the call —
        keyed on first entry, not on task index, so it survives applicability
        skipping — and is consumed exactly once."""
        opening = (self.greeting or intro) if not self._opened else intro
        self._opened = True
        return opening

    def note_task_entered(self, index: int) -> None:
        self.active_task_index = index
        self._write_cursor(self.plan.tasks[index].task_key)

    def note_wrap_up_entered(self) -> None:
        self.active_task_index = None
        self._write_cursor(WRAP_UP_TASK_KEY)

    # -- Phase-2 seams ----------------------------------------------------------

    def attach_session(self, session: AgentSession[TakeoverState]) -> None:
        """Hand the controller the session so a rule-engine directive can interrupt/swap
        the bot. Wired during entrypoint setup, BEFORE session.start — this only stores
        the reference; nothing drives the session until the Observer extracts an answer,
        which requires a started session producing transcript turns."""
        self._session = session

    def update_answers(self, answers: dict[str, Any]) -> None:
        """Refresh the in-process answers snapshot (Observer-fed in Phase 2)."""
        self._answers = dict(answers)

    async def apply_directive_now(self, directive: Directive) -> None:
        """Apply a rule-engine redirect immediately, from the Observer's background task:
        interrupt the bot (it goes silent, cutting off any in-flight speech), then swap
        agent (terminate → wrap-up / skip → target task) or re-ask (contradiction).

        Serialized on the controller lock against a `task_complete` handoff; a skip whose
        target is no longer ahead of the active task is dropped. Never raises into the
        caller — a redirect must not drop the call. No-op while a human supervisor has taken
        over — the rule engine must not yank the agent around under a live takeover."""
        if self._session is None or takeover_engaged(self._session):
            return
        try:
            async with self.lock:
                if self.active_task_index is None:
                    return
                if isinstance(directive, ReAsk):
                    await self._session.interrupt()
                    self._session.generate_reply(instructions=self._reask_instruction(directive))
                    return
                target = self._directive_target(directive)
                if target is None:  # skip whose target is already at/behind us → no-op
                    return
                await self._session.interrupt()
                self._session.update_agent(target)
        except Exception as exc:
            logger.warning(
                "plan run %s: directive apply failed (%s)", self.room_name, type(exc).__name__
            )

    def _directive_target(self, directive: Terminate | SkipToTask) -> Agent | None:
        if isinstance(directive, Terminate):
            return self.wrap_up_agent
        for i, task in enumerate(self.plan.tasks):
            if task.task_key == directive.task_key:
                # Only skip forward: a target at or behind the active task is a no-op.
                if self.active_task_index is not None and i <= self.active_task_index:
                    return None
                return self.agents[i]
        return None  # unknown task_key (should not happen: validated at compile)

    @staticmethod
    def _reask_instruction(directive: ReAsk) -> str:
        clarify = f" {directive.clarify}" if directive.clarify else ""
        return f"CONSISTENCY CHECK: {directive.reason} Re-ask to clarify, do not move on.{clarify}"

    # -- internals ----------------------------------------------------------------

    def _agent_at(self, index: int | None) -> Agent:
        return self.agents[index] if index is not None else self.wrap_up_agent

    def _next_applicable(self, start: int) -> int | None:
        for i in range(start, len(self.plan.tasks)):
            cond = self.plan.tasks[i].applicable_when
            if cond is None or evaluate(cond, self._answers, self.plan.shared_conditions):
                return i
        return None

    def _write_cursor(self, task_key: str) -> None:
        """Fire-and-forget: a Redis blip must never delay the agent's speech."""

        async def write() -> None:
            try:
                await self._run_state.set_active_task(self.room_name, task_key)
            except Exception as exc:  # isolate the voice path from any Redis failure
                logger.warning(
                    "plan run %s: cursor write failed (%s)", self.room_name, type(exc).__name__
                )

        task = asyncio.create_task(write())
        self._cursor_writes.add(task)
        task.add_done_callback(self._cursor_writes.discard)

    async def drain_cursor_writes(self) -> None:
        """Await in-flight cursor writes (test hook; also usable at shutdown)."""
        while self._cursor_writes:
            await asyncio.gather(*list(self._cursor_writes), return_exceptions=True)
