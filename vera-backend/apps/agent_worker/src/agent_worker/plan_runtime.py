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
from typing import Any, cast

from livekit.agents import Agent, AgentSession, llm
from livekit.agents.llm import ChatItem
from opentelemetry import trace

from agent_worker.agent import VeraAgent
from agent_worker.directives import Directive, ReAsk, SkipToTask, Terminate
from agent_worker.handoff import carry_chat_ctx, carry_items, own_items
from agent_worker.intervention import TakeoverState, takeover_engaged
from agent_worker.prompt import CARTESIA_MARKUP_GUIDE, SCOPE_DISCIPLINE
from vera_core.forms.call_plan import CallPlan, PlanFieldDescriptor
from vera_core.forms.conditions import evaluate, is_applicable, is_required
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
_OPENING_DIRECTIVE = (
    "Continue the call now by asking the first question of the current task that the "
    "representative has not already answered. Do not re-ask anything already on file — "
    "confirm those instead."
)


def _gap_block(title: str) -> str:
    """Static instruction block for a gap agent (built up front, so only the task
    title is known). The specific still-missing fields are injected live in
    `on_enter` — the answer snapshot is only known once the call is running."""
    return (
        f"# Current task: Follow-up questions ({title})\n"
        "A few required questions from earlier in the call are still unanswered. "
        "When prompted, re-ask ONLY those specific questions — concisely, politely, "
        "one at a time. If the representative cannot answer, accept it and move on; "
        "never press or repeat. This is a mid-call follow-up, NOT the end of the call: "
        "do NOT say goodbye, do NOT thank the representative as if finishing, and do NOT "
        "claim you have everything you need — more questions may still follow. Once you "
        "have re-asked the listed questions, simply call gap_complete."
    )


def _gap_reask_instruction(fields: list[PlanFieldDescriptor]) -> str:
    """Live re-ask directive listing the still-missing required fields."""
    lines: list[str] = []
    for field in fields:
        line = f"- {field.title}"
        if field.values:
            line += f" (expected one of: {', '.join(field.values)})"
        lines.append(line)
    listed = "\n".join(lines)
    return (
        "I have a couple of quick follow-up questions. Re-ask the representative for these "
        "still-missing required details, briefly and naturally — do not imply the call is "
        f"ending:\n{listed}"
    )


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
            id=self._task.task_key,
        )

    async def on_enter(self) -> None:
        self._controller.note_task_entered(self._task_index)
        if takeover_engaged(self.session):
            logger.info("task entered under supervisor takeover; staying silent")
            return
        # Read before opening_line — that call flips `opened` as a side effect.
        is_opening_turn = not self._controller.opened
        opening = self._controller.opening_line(self._task.intro)
        if opening:
            # Awaited, so the lead below can never be queued on top of in-flight TTS.
            await self.session.say(opening).wait_for_playout()
        if not is_opening_turn:
            # The call's opening turn belongs to the rep — they answer the greeting
            # first. Every later swap leads proactively so it never lands in silence.
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
        # Carry the conversation into the successor — LiveKit doesn't for a
        # tool-returned agent, so without this it re-greets and re-asks.
        await self._controller.prepare_successor(self, successor)
        self._tag_task_complete_handoff(successor)
        return successor

    def _tag_task_complete_handoff(self, successor: Agent) -> None:
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.handoff.from_task": self._task.task_key,
                    "vera.handoff.to_task": successor.id,
                    "vera.handoff.reason": "task_complete",
                }
            )
        except Exception as exc:
            logger.warning(
                "plan run %s: task-complete handoff span tagging failed (%s)",
                self._controller.room_name,
                type(exc).__name__,
            )
        logger.info("handoff: %s -> %s (reason=task_complete)", self._task.task_key, successor.id)


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
            id=WRAP_UP_TASK_KEY,
        )

    async def on_enter(self) -> None:
        self._controller.note_wrap_up_entered()  # the cursor still records where we parked
        if takeover_engaged(self.session):
            logger.info("wrap-up entered under supervisor takeover; skipping the goodbye")
            return
        self.session.generate_reply(instructions=_WRAP_UP_DIRECTIVE)


class GapTaskAgent(Agent):
    """End-of-call gap pass for ONE task: re-asks that task's still-missing
    required fields before wrap-up, then hands off to the next gapped task (or
    wrap-up). Dialogue-only like PlanTaskAgent; its sole tool is `gap_complete`."""

    def __init__(self, controller: "PlanRunController", task_index: int) -> None:
        self._controller = controller
        self._task_index = task_index
        self._task = controller.plan.tasks[task_index]
        super().__init__(
            instructions=_instructions(
                controller.plan,
                _gap_block(self._task.title),
                extra_instructions=controller.extra_instructions,
            ),
        )

    @property
    def task_index(self) -> int:
        """Which task this agent sweeps."""
        return self._task_index

    async def on_enter(self) -> None:
        self._controller.note_task_entered(self._task_index)
        if takeover_engaged(self.session):
            return
        fields = self._controller.gap_fields(self._task_index)
        if not fields:
            successor = await self._controller.advance_gap_from(self._task_index)
            await self._controller.prepare_successor(self, successor)
            self.session.update_agent(successor)
            return
        self.session.generate_reply(instructions=_gap_reask_instruction(fields))

    @llm.function_tool(
        name="gap_complete",
        description=(
            "Finish re-asking the outstanding questions and move on. Call this once "
            "you have re-asked every question you were given — whether or not the "
            "representative could answer them."
        ),
    )
    async def _gap_complete(self) -> Agent | str:
        if takeover_engaged(self.session):
            return "A human supervisor has taken over this call. Stay silent."
        successor = await self._controller.advance_gap_from(self._task_index)
        await self._controller.prepare_successor(self, successor)
        return successor


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
        gap_pass_enabled: bool = True,
        previous_task_only: bool = True,
    ) -> None:
        if not plan.tasks:
            raise ValueError("call plan has no tasks")
        self.plan = plan
        self.room_name = room_name
        self.greeting = greeting
        # Tenant persona-tweak overlay, appended to every plan agent's instructions.
        self.extra_instructions = extra_instructions
        self.gap_pass_enabled = gap_pass_enabled
        # Carry only the previous task's own turns across a handoff instead of the whole
        # call — the default. False restores the cumulative pre-window behavior.
        self.previous_task_only = previous_task_only
        # The item ids each agent was HANDED, so its own turns are everything else in its
        # context. Keyed by agent identity, which is why it can't be the usual task index:
        # a PlanTaskAgent and its GapTaskAgent share one.
        self._boundaries: dict[Agent, frozenset[str]] = {}
        self._opened = False  # has any task agent spoken the call's opening line yet
        self._run_state = run_state
        # In-process answers snapshot for applicability/skip decisions, seeded
        # with the form's intake prefill (so gates work from call start); the
        # Phase-2 Observer keeps it current. Redis stays the cross-process truth.
        self._answers: dict[str, Any] = dict(plan.prefilled)
        self.active_task_index: int | None = None
        # Tasks the call actually entered (main pass or gap pass).
        self._visited_tasks: set[int] = set()
        self._terminated = False
        # How far the call has PROGRESSED, distinct from `active_task_index` (which is the
        # Observer's extraction cursor and moves backwards during the gap pass). Monotonic,
        # so the forward-only skip guard can never redirect into a completed task.
        self._max_task_index = -1
        # The gap pass runs ONCE, immediately before the closing task, so any re-ask
        # happens before that task collects the reference number and says goodbye.
        self._closing_task_index = len(plan.tasks) - 1
        self._gap_pass_done = False
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
        # One gap agent per SWEEPABLE task, built up front like the task agents so a malformed
        # plan fails here at construction rather than mid-call. The closing task is never
        # swept (its reference-number/goodbye stays last), so it gets no gap agent.
        self.gap_agents = [GapTaskAgent(self, i) for i in range(self._closing_task_index)]
        self.wrap_up_agent = WrapUpAgent(self)

    # -- conversation-path API ------------------------------------------------

    def first_agent(self) -> Agent:
        return self._agent_at(self._next_applicable(0))

    async def advance_from(self, index: int) -> Agent:
        async with self.lock:
            self.generation += 1
            next_index = self._next_applicable(index + 1)
            # Right before the closing task, sweep gaps in the visited substantive tasks
            # so any re-ask lands BEFORE the closer's reference-number/goodbye.
            gap_agent = self._maybe_enter_gap_pass(next_index)
            return gap_agent if gap_agent is not None else self._agent_at(next_index)

    async def advance_gap_from(self, index: int) -> Agent:
        async with self.lock:
            self.generation += 1
            gap_index = self._next_gap_task(index + 1)
            if gap_index is not None:
                return self.gap_agents[gap_index]
            self._gap_pass_done = True
            # Re-check applicability like every other transition: the Observer keeps writing
            # answers during the pass, so the closer's gate can have flipped since entry.
            return self._agent_at(self._next_applicable(self._closing_task_index))

    async def prepare_successor(self, source: Agent, target: Agent) -> None:
        """Give `target` the conversation it needs before it becomes active — the single seam
        EVERY agent transition goes through, tool handoff and `update_agent` swap alike.

        Storing the target's resulting item ids is what makes the window work: a path that
        skipped this seam would leave its target with no boundary, and the hop after it would
        carry that agent's whole context again."""
        if not self.previous_task_only:
            self._boundaries[target] = await carry_chat_ctx(source, target)
            return
        self._boundaries[target] = await carry_items(target, self._carry_set(source, target))

    def _carry_set(self, source: Agent, target: Agent) -> list[ChatItem]:
        """Chronological carry set: the swept task's turns for a gap target, then the
        source's own turns. Nothing older — the window is one task deep.

        A gap agent re-asks ITS OWN task's missing fields, so it needs that task's turns —
        mid-call ones, not those of the chronological predecessor it arrives from (the gap
        pass walks backwards from just before the closing task)."""
        carried: list[ChatItem] = []
        if isinstance(target, GapTaskAgent):
            swept = self.agents[target.task_index]
            carried += self._own_turns(swept)
        return [*carried, *self._own_turns(source)]

    def _own_turns(self, agent: Agent) -> list[ChatItem]:
        """What `agent` contributes to a successor's window.

        Normally its own turns. Two cases must pass their WHOLE context through instead, or the
        window swallows the conversation rather than bounding it:

        * a **gap agent** is an inserted hop, not a task, so it must not shadow the substantive
          task the window is meant to carry — the closer would otherwise see only a re-ask;
        * an agent that **said nothing at all** has no own turns, so it would hand its successor
          an EMPTY context. Reachable: `GapTaskAgent.on_enter` swaps on without speaking when the
          Observer answered its fields between selection and entry, and that successor is the
          closer, which collects the reference number and says goodbye.
        """
        own = own_items(agent, self._boundaries.get(agent, frozenset()))
        if own and not isinstance(agent, GapTaskAgent):
            return own
        return list(agent.chat_ctx.items)

    @property
    def opened(self) -> bool:
        """Whether the call's opening line has been spoken yet."""
        return self._opened

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
        self._max_task_index = max(self._max_task_index, index)
        self._visited_tasks.add(index)
        task_key = self.plan.tasks[index].task_key
        self._tag_task_entry(task_key, index)
        self._write_cursor(task_key)

    def note_wrap_up_entered(self) -> None:
        self.active_task_index = None
        self._tag_task_entry(WRAP_UP_TASK_KEY, None)
        self._write_cursor(WRAP_UP_TASK_KEY)

    def _tag_task_entry(self, task_key: str, index: int | None) -> None:
        try:
            attrs: dict[str, str | int] = {"vera.task.key": task_key}
            if index is not None:
                attrs["vera.task.index"] = index
            trace.get_current_span().set_attributes(attrs)
        except Exception as exc:
            logger.warning(
                "plan run %s: task-entry span tagging failed (%s)",
                self.room_name,
                type(exc).__name__,
            )

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
                self._tag_rule_handoff(target)
                if isinstance(directive, Terminate):
                    # A terminate_call flow rule ends the call: no end-of-call gap pass.
                    self._terminated = True
                await self._session.interrupt()
                # Same seam as a tool handoff: this path used to swap with no carry at all.
                await self.prepare_successor(self._session.current_agent, target)
                self._session.update_agent(target)
        except Exception as exc:
            logger.warning(
                "plan run %s: directive apply failed (%s)", self.room_name, type(exc).__name__
            )

    def _tag_rule_handoff(self, target: Agent) -> None:
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.handoff.from_task": self.plan.tasks[
                        cast(int, self.active_task_index)
                    ].task_key,
                    "vera.handoff.to_task": target.id,
                    "vera.handoff.reason": "flow_rule",
                }
            )
        except Exception as exc:
            logger.warning(
                "plan run %s: rule-handoff span tagging failed (%s)",
                self.room_name,
                type(exc).__name__,
            )

    def _directive_target(self, directive: Terminate | SkipToTask) -> Agent | None:
        if isinstance(directive, Terminate):
            return self.wrap_up_agent
        for i, task in enumerate(self.plan.tasks):
            if task.task_key == directive.task_key:
                # Only skip forward, measured against PROGRESS not the active cursor:
                if i <= self._max_task_index:
                    return None
                return self.agents[i]
        return None  # unknown task_key (should not happen: validated at compile)

    @staticmethod
    def _reask_instruction(directive: ReAsk) -> str:
        clarify = f" {directive.clarify}" if directive.clarify else ""
        return f"CONSISTENCY CHECK: {directive.reason} Re-ask to clarify, do not move on.{clarify}"

    # -- gap-pass API -------------------------------------------------------------

    def gap_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """A task's still-open gaps: applicable (gates hold) ∧ required ∧ unanswered,
        against the live answer snapshot — the same required/applicable set the form's
        completion percentage counts."""
        shared = self.plan.shared_conditions
        return [
            field
            for field in self.plan.tasks[task_index].fields
            if is_applicable(field.gates, self._answers, shared)
            and is_required(field, self._answers, shared)
            and not self._is_answered(field.path)
        ]

    def _is_answered(self, path: str) -> bool:
        value = self._answers.get(path)
        return value is not None and str(value).strip() != ""

    def _maybe_enter_gap_pass(self, next_index: int | None) -> Agent | None:
        """When the next task is the closing task, divert into the gap pass (once) over
        the earlier visited tasks. Returns the first gap agent, or None to proceed
        normally — no gaps, disabled, already run, terminated, or the closer isn't next.
        A single-task plan therefore never sweeps: `next_index` can never be 0."""
        if (
            not self.gap_pass_enabled
            or self._terminated
            or self._gap_pass_done
            or next_index != self._closing_task_index
        ):
            return None
        gap_index = self._next_gap_task(0)
        if gap_index is None:
            self._gap_pass_done = True
            return None
        return self.gap_agents[gap_index]

    def _next_gap_task(self, start: int) -> int | None:
        """Next VISITED, applicable task in `[start, closing_task)` that still has gap
        fields. Restricting to visited tasks respects flow-rule redirects (a task skipped
        by applicability or a `skip_to_task` was never entered, so it is never swept);
        stopping before the closing task keeps its reference-number/goodbye last."""
        for i in range(start, self._closing_task_index):
            if i not in self._visited_tasks:
                continue
            cond = self.plan.tasks[i].applicable_when
            if cond is not None and not evaluate(cond, self._answers, self.plan.shared_conditions):
                continue
            if self.gap_fields(i):
                return i
        return None

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
