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
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from livekit.agents import Agent, AgentSession, llm
from livekit.agents.llm import ChatItem
from opentelemetry import trace

from agent_worker.agent import VeraAgent
from agent_worker.coaching import apply_pending_coaching_notes
from agent_worker.directives import Directive, ReAsk, SkipToTask, Terminate
from agent_worker.handoff import carry_chat_ctx, carry_items, own_items
from agent_worker.intervention import TakeoverState, takeover_engaged
from agent_worker.prompt import (
    CARTESIA_MARKUP_GUIDE,
    CLOSING_DISCIPLINE,
    HANDOFF_DISCIPLINE,
    SCOPE_DISCIPLINE,
    TOOL_REASON_ARG,
)
from vera_core.forms.call_plan import CallPlan, PlanFieldDescriptor, gating_seed, owed_now
from vera_core.forms.conditions import (
    alternative_index,
    evaluate,
    has_value,
    is_applicable,
    is_required,
)
from vera_core.forms.dsl import AllCondition, AnyCondition, Condition, NotCondition, RefCondition
from vera_core.forms.prompting import numbered_questions, render_panels
from vera_core.forms.question_plan import PromptPanel, drop_questions
from vera_core.plan_store import PlanRunStateService

if TYPE_CHECKING:
    from agent_worker.observer import ObserverManager

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

# Starts a gap sweep. Only a lead-in: the questions themselves live in the instructions,
# where they survive past this one inference (see _gap_block).
_GAP_REASK_DIRECTIVE = (
    "Begin the follow-up questions now: ask the first question on the current task's numbered "
    "list, naturally and without implying the call is ending."
)

# Consecutive gap_complete refusals that shrink nothing before the guard gives up.
_GAP_FRUITLESS_REFUSALS = 2

# Consecutive task_complete refusals that shrink nothing before the guard gives up.
_TASK_FRUITLESS_REFUSALS = 2


def _gap_block(title: str, fields: list[PlanFieldDescriptor]) -> str:
    """Instruction block for a gap agent, listing every question it still owes."""
    if fields:
        count = len(fields)
        subject = "question is" if count == 1 else "questions are"
        owed = (
            f"{count} required {subject} still unanswered from earlier in the call. Re-ask ONLY "
            "the questions on this numbered list, politely, one at a time, and keep going until "
            "every item on it has been asked — the list is the complete set:\n"
            f"{_field_lines(fields, numbered=True)}"
        )
    else:
        owed = (
            "Required questions from earlier in the call are still unanswered. When the list "
            "arrives, re-ask ONLY those specific questions, politely, one at a time."
        )
    return (
        f"# Current task: Follow-up questions ({title})\n"
        f"{owed}\n"
        "Keep each question brief, but do not shorten the LIST — every question on it is owed. "
        "If the representative cannot answer one, accept it and move to the next one on the list; "
        "never press or repeat. This is a mid-call follow-up, NOT the end of the call: "
        "do NOT say goodbye, do NOT thank the representative as if finishing, and do NOT "
        "claim you have everything you need — more questions may still follow. Once every "
        "question on the list has been asked, call gap_complete."
    )


def _completeness_block(panels: list[PromptPanel]) -> str:
    """This task's LOWER bound: ask every question on the list above before completing.

    Nothing else in a task agent's instructions states one — `SCOPE_DISCIPLINE` bounds the list
    from above and `HANDOFF_DISCIPLINE` governs only timing — so the requirement lived solely in
    `task_complete`'s tool description, and a live call ended every task early. `_gap_block`
    states this same bound and its agent did not, which is why the wording mirrors it, and why
    this sits directly under the list rather than after the trailing rules — one of which tells
    the agent to skip the remaining questions when its condition holds.

    The total is `render_panels`' last ordinal, so the agent can check the claim against the
    list it can see instead of taking it on faith."""
    total = numbered_questions(panels)
    if not total:
        return ""
    if total == 1:
        return (
            "COMPLETENESS\n"
            "The list above is exactly 1 question and is the complete set. Ask it, then call "
            "task_complete once it has been asked — whether or not the representative could "
            "answer it, and never before."
        )
    return (
        "COMPLETENESS\n"
        f"The list above runs 1 to {total} and is the complete set — every question on it is "
        "owed, and the section headings group them without breaking the count. Ask every one of "
        f"them, one at a time, in order. Call task_complete only once all {total} have been "
        "asked, whether or not the representative could answer them, and never while one of them "
        "is still unasked."
    )


def _owning_segment(path: str) -> str:
    """The segment that owns the leaf, e.g. `...labs.cpt_58340.covered` → `cpt_58340`."""
    parts = path.split(".")
    return parts[-2] if len(parts) > 1 else path


def _field_line(
    field: PlanFieldDescriptor, title_counts: Counter[str], *, marker: str = "-"
) -> str:
    """One field, disambiguated when its title repeats in the list it's rendered into.

    Titles are not unique — every CPT code's field is titled "Covered" — so a bare-title
    line names nothing the agent could act on; every field-list renderer shares this."""
    line = f"{marker} {field.title}"
    if title_counts[field.title] > 1:
        line += f" ({_owning_segment(field.path)})"
    if field.values:
        line += f" (expected one of: {', '.join(field.values)})"
    return line


def _field_lines(fields: list[PlanFieldDescriptor], *, numbered: bool = False) -> str:
    """Questions as a list, naming the expected values where the schema fixes them.

    `numbered` ordinals give the agent something to check itself against: a CPT-heavy task
    renders a run of near-identical lines, and a bulleted run of fourteen carries no signal
    that fourteen is how many were owed."""
    title_counts = Counter(field.title for field in fields)
    return "\n".join(
        _field_line(field, title_counts, marker=f"{position}." if numbered else "-")
        for position, field in enumerate(fields, start=1)
    )


def _is_message(item: ChatItem, role: str) -> bool:
    """Whether `item` is a spoken message from `role` — a function call or its output is not."""
    return item.type == "message" and item.role == role


def _instructions(plan: CallPlan, task_block: str, *, extra_instructions: str | None) -> str:
    """Session block (+ the form's Known-information prefill, + the tenant's
    persona-tweak extra instructions, when present) + one task-specific block +
    the discipline guardrails + the Cartesia TTS markup guide — fused once, at
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
    parts.append(HANDOFF_DISCIPLINE)
    parts.append(CLOSING_DISCIPLINE)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)


class PlanTaskAgent(Agent):
    """One schema task's conversation. Dialogue-only by construction: its tools
    (`task_complete`, `representative_requests_end_call`) only route the call — neither
    writes an answer — and its sole shared-state write is the cursor."""

    def __init__(self, controller: "PlanRunController", task_index: int) -> None:
        self._controller = controller
        self._task_index = task_index
        self._task = controller.plan.tasks[task_index]
        self._task_block = f"# Current task: {self._task.title}\n{self._task.prompt}"
        # The task's own question total as rendered at ENTRY — the same count COMPLETENESS
        # states, not the answer-sensitive `owed_question_count` (a task whose follow-ups sit
        # behind an unanswered gate reads as owing almost nothing at entry, collapsing this to
        # a number far short of what the agent was actually told to ask). The ceiling must also
        # measure the same window as `_rep_turns` (which accumulates over the whole task);
        # comparing it against the CURRENTLY outstanding count let an 11-turn task clear a
        # 5-question bar and hand off with six questions unasked.
        self._questions_at_entry = 0
        self._refusals = 0
        self._outstanding_at_last_refusal: int | None = None
        self._rep_turns = 0
        self._advanced_this_turn = False
        super().__init__(instructions=self._build_instructions(), id=self._task.task_key)

    def _build_instructions(self, panels: list[PromptPanel] | None = None) -> str:
        """This task's full instruction text; `panels` replaces the compiled question list when
        the gates rule some questions out on this call.

        The completeness rule is counted off the SAME panels the list is rendered from, so a
        narrowed task states the total the agent can actually see rather than the compiled one."""
        return _instructions(
            self._controller.plan,
            self._assembled_block(self._task.panels if panels is None else panels),
            extra_instructions=self._controller.extra_instructions,
        )

    def _assembled_block(self, panels: list[PromptPanel]) -> str:
        """The task block rebuilt from the pieces the compiler shipped, with the completeness
        rule seated directly under the question list.

        Reassembled rather than string-edited because recovering the pieces by splitting
        `prompt` dropped the TERMINATION RULE and CONSISTENCY CHECK blocks that follow the list
        (`TestPanelsMatchThePrompt` pins the reassembly as byte-identical). A task the compiler
        shipped no tree for keeps its `prompt` verbatim — there is nothing to count or narrow."""
        task = self._task
        if not task.panels:
            return self._task_block
        parts = (task.lead_in, render_panels(panels), _completeness_block(panels), task.trailing)
        return f"# Current task: {task.title}\n" + "\n\n".join(p for p in parts if p)

    async def on_enter(self) -> None:
        self._controller.note_task_entered(self._task_index)
        if takeover_engaged(self.session):
            logger.info("task entered under supervisor takeover; staying silent")
            return
        if await self._skip_when_nothing_applies():
            return
        kept = await self._apply_gating()
        self._questions_at_entry = numbered_questions(kept or self._task.panels)
        # Read before opening_line — that call flips `opened` as a side effect.
        is_opening_turn = not self._controller.opened
        opening = self._controller.opening_line(self._task.intro)
        if opening:
            # Awaited, so the lead below can never be queued on top of in-flight TTS.
            handle = self.session.say(opening)
            await handle.wait_for_playout()
            if is_opening_turn:
                # Only the CALL's opening is pinned — a later task's intro is not an introduction.
                self._controller.note_opening_spoken(handle.chat_items)
        if not is_opening_turn:
            # The call's opening turn belongs to the rep — they answer the greeting
            # first. Every later swap leads proactively so it never lands in silence.
            self.session.generate_reply(instructions=_OPENING_DIRECTIVE)

    async def _skip_when_nothing_applies(self) -> bool:
        """Hand straight on, silently, when the gates exclude every question in this task.

        Announcing a section and closing it in the same breath ("Now I'd like to ask about male
        partner coverage… Thanks, that covers the male partner benefits.") sounds broken, and
        asking those questions anyway is worse. A task with NO fields at all is a different thing
        — it carries only speech, so it still runs.

        A CONDITIONAL question counts as a reason to run: its gate is not yet decidable, so
        skipping the task would settle it by omission."""
        if (
            not self._task.fields
            or self._controller.applicable_fields(self._task_index)
            or self._controller.conditional_fields(self._task_index)
        ):
            return False
        if not self._controller.opened:
            # The call's greeting and recording disclosure ride on the opening task's intro;
            # skipping silently here would drop them from the call altogether.
            return False
        logger.info("task %s skipped: every question is gated out", self._task.task_key)
        # Nothing was spoken, so the closer must still sign off (see note_task_outro).
        self._controller.note_task_outro(None)
        successor = await self._controller.advance_from(self._task_index)
        await self._controller.prepare_successor(self, successor)
        self.session.update_agent(successor)
        return True

    async def _apply_gating(self) -> list[PromptPanel]:
        """Re-render this task's question list without the questions the gates rule out,
        and return the panels actually rendered — `on_enter` snapshots its question-count
        ceiling off this SAME list, so the two can never diverge.

        The list the agent reads IS the list it should ask, so a gated-out question is simply
        absent. The old shape — list every question, then append "do NOT ask these" underneath
        — needed the agent to reconcile two contradictory lists, and `SCOPE_DISCIPLINE` tells
        it the list is complete.

        Judged on decidably-false gates only (`excluded_fields`): a question whose gate is
        still undecided stays in the list carrying its own prose gate, which the agent
        re-evaluates every turn. That is why there is no separate "conditional" block — the
        condition is already stated on the question it governs.

        Rebuilt from the compiled tree rather than edited in place, so a re-entry (a ReAsk
        directive) re-narrows against fresher answers instead of stacking."""
        excluded = self._controller.excluded_fields(self._task_index)
        if not excluded or not self._task.panels:
            return self._task.panels
        kept = drop_questions(self._task.panels, {f.path for f in excluded})
        await self.update_instructions(self._build_instructions(kept))
        return kept

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        apply_pending_coaching_notes(self.session, turn_ctx)
        self._rep_turns += 1
        self._advanced_this_turn = False

    @llm.function_tool(
        name="task_complete",
        description=(
            "Move on to the next task of the call. Call this ONLY when every "
            "question in the current task has been answered or the representative "
            "has confirmed they cannot answer what remains. Never call it to skip "
            "questions that are still answerable, and never in the same turn as a "
            "question — the representative's answer must arrive first. " + TOOL_REASON_ARG
        ),
    )
    async def _task_complete(self, reason: str) -> Agent | str:
        if (blocked := self._refuse_handoff()) is not None:
            return blocked
        refusal = self._refuse_premature_completion()
        self._tag_completion_decision(refusal)
        if refusal is not None:
            return refusal
        # Set before the first await: LiveKit runs every function call from one LLM
        # response as its own task, so a second task_complete can reach this guard
        # while the first is still suspended below — the marker must already be up.
        self._advanced_this_turn = True
        outro = self._task.outro
        self._controller.note_task_outro(outro)
        if outro:
            # Exit speech first; LiveKit drains queued speech through the swap.
            self.session.say(outro)
        successor = await self._controller.advance_from(self._task_index)
        # Carry the conversation into the successor — LiveKit doesn't for a
        # tool-returned agent, so without this it re-greets and re-asks.
        await self._controller.prepare_successor(self, successor)
        self._tag_handoff(successor, reason="task_complete")
        return successor

    # The rep's own words are the trigger, so the rule lives where the model decides: this
    # description. The "hold on" carve-out is the list the introduction task already waits through.
    @llm.function_tool(
        name="representative_requests_end_call",
        description=(
            "The representative has asked to end the call — they must go, they have another "
            "call, or they asked to wrap up. Call this IMMEDIATELY, in that same turn: it "
            "abandons every remaining question and moves straight to the closing questions "
            "(their name and a call reference number). Never call it for 'hold on', 'let me "
            "check', 'one moment' or any other pause, and never because the current task is "
            "finished — that is what task_complete is for. " + TOOL_REASON_ARG
        ),
    )
    async def _request_end_call(self, reason: str) -> Agent | str:
        """Abandon the rest of the plan and hand to the closing task, which collects the
        representative's name and the call reference number and then ends the call.

        Deliberately skips `_refuse_premature_completion`: giving up the questions this task
        still owes is the point, so the guard that protects them is wrong here."""
        if (blocked := self._refuse_handoff()) is not None:
            return blocked
        if self._task_index >= self._controller.closing_task_index:
            return "You are already on the closing questions — finish them, then task_complete."
        # Set before the first await, for the reason given in `_task_complete`.
        self._advanced_this_turn = True
        # No outro: announcing "I have everything I need" to a rep who just asked to go is a lie,
        # and clearing a stale sign-off keeps the goodbye if the closer is gated out.
        self._controller.note_task_outro(None)
        successor = await self._controller.request_closing_task()
        await self._controller.prepare_successor(self, successor)
        self._tag_handoff(successor, reason="end_call_requested")
        return successor

    def _refuse_handoff(self) -> str | None:
        """Why this turn must not route the call onward at all — shared by every routing tool."""
        if takeover_engaged(self.session):
            # A str is a tool result, so the plan parks here. Returning `self` would
            # re-fire on_enter and speak the intro again.
            return "A human supervisor has taken over this call. Stay silent."
        if self._advanced_this_turn:
            # A second chain-advancing call in one turn traverses a task without ever
            # entering its question loop. Inert, not a second Agent.
            return "Already moving on — continue with the next question."
        return None

    def _refuse_premature_completion(self) -> str | None:
        """Send the agent back for this task's still-open required questions.

        Two bounds, because a rep who cannot answer never empties `gap_fields` and an
        unconditional guard would strand the plan on this task:

        * a turn ceiling measured over the SAME window on both sides — rep turns across the
          task against the questions owed when the task was entered. N questions cannot be
          asked in fewer than N exchanges;
        * a refusal budget, since the Observer extracts in a detached pass and the answer to
          the task's last question is never on file here. Progress — the outstanding set
          shrank — resets it, so a task still landing answers keeps its runway.
        """
        outstanding = self._controller.gap_fields(self._task_index)
        if not outstanding:
            return None
        if self._rep_turns >= self._questions_at_entry:
            return None
        shrank = (
            self._outstanding_at_last_refusal is None
            or len(outstanding) < self._outstanding_at_last_refusal
        )
        self._refusals = 0 if shrank else self._refusals + 1
        if self._refusals >= _TASK_FRUITLESS_REFUSALS:
            logger.info(
                "task %s advancing with %d question(s) still open",
                self._task.task_key,
                len(outstanding),
            )
            return None
        self._outstanding_at_last_refusal = len(outstanding)
        logger.info(
            "task %s: completion refused, %d required question(s) still open",
            self._task.task_key,
            len(outstanding),
        )
        return (
            "Not yet — these required questions of the current task have no answer on file. "
            "Ask the representative for them now (one at a time), and call task_complete once "
            "they are answered or the representative says they cannot answer:\n"
            f"{_field_lines(outstanding)}"
        )

    def _tag_completion_decision(self, refusal: str | None) -> None:
        """Owed-question count and refusal outcome, tagged on EVERY `task_complete` call —
        including one that advances anyway (turn ceiling / refusal budget spent) with fields
        still owed, the silently-missed-question failure a model-authored `reason` used to hide."""
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.completion.owed_count": len(
                        self._controller.gap_fields(self._task_index)
                    ),
                    "vera.completion.refused": refusal is not None,
                }
            )
        except Exception as exc:
            logger.warning(
                "plan run %s: task-complete decision span tagging failed (%s)",
                self._controller.room_name,
                type(exc).__name__,
            )

    def _tag_handoff(self, successor: Agent, *, reason: str) -> None:
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.handoff.from_task": self._task.task_key,
                    "vera.handoff.to_task": successor.id,
                    "vera.handoff.reason": reason,
                }
            )
        except Exception as exc:
            logger.warning(
                "plan run %s: handoff span tagging failed (%s)",
                self._controller.room_name,
                type(exc).__name__,
            )
        logger.info("handoff: %s -> %s (reason=%s)", self._task.task_key, successor.id, reason)


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
        if self._controller.signed_off:
            # The closing task's outro already said goodbye, so there is nothing left to say and
            # nothing to decide. Hanging up here rather than asking the LLM to do it silently is
            # what makes the single sign-off deterministic: the same instruction was obeyed in two
            # of three eval scenarios and ignored in the third, which spoke a second goodbye.
            self.close_call()
            return
        self.session.generate_reply(instructions=_WRAP_UP_DIRECTIVE)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        apply_pending_coaching_notes(self.session, turn_ctx)


class GapTaskAgent(Agent):
    """End-of-call gap pass for ONE task: re-asks that task's still-missing
    required fields before wrap-up, then hands off to the next gapped task (or
    wrap-up). Dialogue-only like PlanTaskAgent; its sole tool is `gap_complete`."""

    def __init__(self, controller: "PlanRunController", task_index: int) -> None:
        self._controller = controller
        self._task_index = task_index
        self._task = controller.plan.tasks[task_index]
        # Questions owed on entry (0 until on_enter, where the answer snapshot exists) and the
        # list currently in the instructions, rewritten whenever that set shrinks.
        self._questions_owed = 0
        self._listed_paths: tuple[str, ...] = ()
        self._rep_turns = 0
        self._outstanding_at_last_refusal: int | None = None
        self._fruitless_refusals = 0
        self._advanced_this_turn = False
        super().__init__(instructions=self._build_instructions([]))

    def _build_instructions(self, fields: list[PlanFieldDescriptor]) -> str:
        """This sweep's full instruction text, listing the questions it still owes."""
        return _instructions(
            self._controller.plan,
            _gap_block(self._task.title, fields),
            extra_instructions=self._controller.extra_instructions,
        )

    @property
    def task_index(self) -> int:
        """Which task this agent sweeps."""
        return self._task_index

    async def on_enter(self) -> None:
        self._controller.note_task_entered(self._task_index)
        if takeover_engaged(self.session):
            return
        # LiveKit drains queued speech BEFORE this activity starts (agent_session.py's
        # update_agent awaits activity.drain), so the preceding task's outro has already
        # finished playing by the time we get here — this wait is audible dead air, bounded
        # by the drain timeout, accepted because a phantom re-ask is worse.
        await self._controller.drain_observer()
        fields = self._controller.gap_fields(self._task_index)
        if not fields:
            successor = await self._controller.advance_gap_from(self._task_index)
            await self._controller.prepare_successor(self, successor)
            self.session.update_agent(successor)
            return
        self._questions_owed = self._controller.owed_question_count(self._task_index)
        await self._apply_gap_list(fields)
        self.session.generate_reply(instructions=_GAP_REASK_DIRECTIVE)

    async def _apply_gap_list(self, fields: list[PlanFieldDescriptor]) -> None:
        """Put this sweep's outstanding questions in the INSTRUCTIONS, where they outlive the
        turn that named them — the `_apply_gating` seam, and rebuilt not appended for the
        reason given there."""
        paths = tuple(field.path for field in fields)
        if paths == self._listed_paths:
            return
        self._listed_paths = paths
        await self.update_instructions(self._build_instructions(fields))

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        apply_pending_coaching_notes(self.session, turn_ctx)
        self._rep_turns += 1
        self._advanced_this_turn = False
        # The Observer keeps writing during the pass, so re-narrow as answers land. Not when the
        # set empties: the agent's next move is then gap_complete, which the guard passes.
        if outstanding := self._controller.gap_fields(self._task_index):
            await self._apply_gap_list(outstanding)

    @llm.function_tool(
        name="gap_complete",
        description=(
            "Finish re-asking the outstanding questions and move on. Call this once "
            "you have re-asked every question you were given — whether or not the "
            "representative could answer them — and never in the same turn as a "
            "question, since their answer must arrive first. " + TOOL_REASON_ARG
        ),
    )
    async def _gap_complete(self, reason: str) -> Agent | str:
        if takeover_engaged(self.session):
            return "A human supervisor has taken over this call. Stay silent."
        if self._advanced_this_turn:
            # A second chain-advancing call in one turn traverses a task without ever
            # entering its question loop. Inert, not a second Agent.
            return "Already moving on — continue with the next question."
        if (refusal := self._refuse_premature_gap_complete()) is not None:
            return refusal
        # Set before the first await — see the matching comment in
        # PlanTaskAgent._task_complete for why the ordering matters.
        self._advanced_this_turn = True
        successor = await self._controller.advance_gap_from(self._task_index)
        await self._controller.prepare_successor(self, successor)
        return successor

    def _refuse_premature_gap_complete(self) -> str | None:
        """Send the sweep back for the questions it has not asked yet, re-listing them.

        The prompt can only make full coverage likely; this is what enforces it. The main pass
        has its own turn ceiling and refusal budget (see `_refuse_premature_completion`) — but
        nothing backstops this one, and it runs once, so whatever it leaves unasked is
        unreachable for the rest of the call.

        Two bounds, because a rep who cannot answer never empties `gap_fields` and an
        unconditional guard would strand the call here:

        * a turn ceiling, for the rep with no answer to give — N questions cannot be asked in
          fewer than N rep exchanges, so once the sweep has had as many turns as it owed, stop
          second-guessing it. Coarse until an answer records WHICH question was asked: the
          Observer attributes answers to a task, but nothing tracks what the bot actually spoke;
        * a fruitless-refusal budget, for the model that calls gap_complete straight back off the
          forced follow-up with no rep turn in between. Progress resets it, so a sweep that is
          still landing answers keeps its runway.
        """
        outstanding = self._controller.gap_fields(self._task_index)
        if not outstanding:
            return None
        owed = self._questions_owed
        if self._rep_turns >= owed:
            return None
        shrank = (
            self._outstanding_at_last_refusal is None
            or len(outstanding) < self._outstanding_at_last_refusal
        )
        self._fruitless_refusals = 0 if shrank else self._fruitless_refusals + 1
        if self._fruitless_refusals >= _GAP_FRUITLESS_REFUSALS:
            logger.info(
                "gap sweep of task %s advancing with %d question(s) still open",
                self._task.task_key,
                len(outstanding),
            )
            return None
        self._outstanding_at_last_refusal = len(outstanding)
        logger.info(
            "gap sweep of task %s: completion refused, %d field(s) open across %d owed ask(s)",
            self._task.task_key,
            len(outstanding),
            owed,
        )
        return (
            f"Not yet — {len(outstanding)} of the follow-up questions you were given still have "
            "no answer on file. Ask the representative for them now, one at a time, and call "
            "gap_complete only once every one of them has been asked:\n"
            f"{_field_lines(outstanding, numbered=True)}"
        )


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
        # Built once: the either/or groups the compiler recovered from the authored sets.
        # `gap_fields` reads it directly — an alternatives set is ONE tree question, but
        # `is_required` is unconditional on both its members, so answering one side does not
        # make the sibling's own `is_required` false; only consulting the group here does.
        self._alternatives = alternative_index(plan.alternative_pairs)
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
        self._signed_off = False  # did the task that JUST finished speak a closing line?
        # The call's opening (greeting + recording/identity disclosure). Pinned: it leads EVERY
        # carry set while the rest of the window stays one task deep, or a long walk hands the
        # closer a context in which VERA never introduced herself.
        self._anchor_items: list[ChatItem] = []
        self._run_state = run_state
        # Pre-call baseline for gate evaluation: the intake values a gate may legitimately be
        # judged against before the call collects anything. Role-scoped (`gating_seed`), so an
        # ask leaf's prefill can never settle a gate. Immutable for the run.
        self._baseline = gating_seed(plan)
        # Baseline + what the call has collected. Redis stays the cross-process truth.
        self._answers: dict[str, Any] = dict(self._baseline)
        # Collectable path -> the task that asks it. A gate referencing a path in THIS task
        # (or a later one) is undecided at entry; one referencing only earlier tasks is final,
        # answered or gated-out-upstream alike. Paths absent here are context/prefilled.
        self._task_of_path: dict[str, int] = {
            field.path: index for index, task in enumerate(plan.tasks) for field in task.fields
        }
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
        # The Observer, attached from main.py alongside the session (see attach_observer).
        # None in Voice Lab and every test that builds a controller bare — drain_observer
        # is then a no-op.
        self._observer_manager: ObserverManager | None = None
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

    @property
    def closing_task_index(self) -> int:
        """The last task — the one that collects the rep's name and the call reference number."""
        return self._closing_task_index

    async def request_closing_task(self) -> Agent:
        """Jump to the closing task because the representative asked to end the call.

        Nothing has to suppress the gap pass: `_maybe_enter_gap_pass` has one caller,
        `advance_from`, so entering the closer directly never reaches it — and leaving the
        closer doesn't either, since `_next_applicable(closing + 1)` is None."""
        async with self.lock:
            self.generation += 1
            return self._closing_agent()

    async def advance_gap_from(self, index: int) -> Agent:
        async with self.lock:
            self.generation += 1
            gap_index = self._next_gap_task(index + 1)
            if gap_index is not None:
                return self.gap_agents[gap_index]
            self._gap_pass_done = True
            return self._closing_agent()

    async def prepare_successor(self, source: Agent, target: Agent) -> None:
        """Give `target` the conversation it needs before it becomes active — the single seam
        EVERY agent transition goes through, tool handoff and `update_agent` swap alike.

        Storing the target's resulting item ids is what makes the window work: a path that
        skipped this seam would leave its target with no boundary, and the hop after it would
        carry that agent's whole context again.

        The call's opening is ANCHORED here — pinned on first capture, then prepended to every
        carry set, so a seven-task walk still shows the closer that VERA already introduced
        herself."""
        self._ensure_anchor(source)
        if not self.previous_task_only:
            self._boundaries[target] = await carry_chat_ctx(source, target)
            return
        self._boundaries[target] = await carry_items(target, self._carry_set(source, target))

    def _carry_set(self, source: Agent, target: Agent) -> list[ChatItem]:
        """Chronological carry set: the pinned opening, then the swept task's turns for a gap
        target, then the source's own turns. Nothing older — the window is one task deep.

        A gap agent re-asks ITS OWN task's missing fields, so it needs that task's turns —
        mid-call ones, not those of the chronological predecessor it arrives from (the gap
        pass walks backwards from just before the closing task).

        `carry_items` dedupes on id keeping the FIRST occurrence, so on the hop where the opener
        is itself the source the anchor keeps the lead and the later copy drops — the order is
        unchanged from before the anchor existed."""
        carried: list[ChatItem] = []
        if isinstance(target, GapTaskAgent):
            swept = self.agents[target.task_index]
            carried += self._own_turns(swept)
        return [*self._anchor_items, *carried, *self._own_turns(source)]

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

    @property
    def signed_off(self) -> bool:
        """Whether the last task to finish already spoke a closing line."""
        return self._signed_off

    def note_task_outro(self, outro: str | None) -> None:
        """Record whether the task now finishing had an outro to speak.

        ASSIGNED, never accumulated: every task speaks an outro, so a sticky flag would be left
        set by an earlier task and would silence wrap-up on a schema whose closing task authors
        none (`disease_only`'s does not). A terminate directive never reaches here, so that path
        keeps the initial False and still gets a spoken goodbye."""
        self._signed_off = bool(outro)

    def note_opening_spoken(self, items: Sequence[ChatItem]) -> None:
        """Pin the items the call's opening line produced, by identity — called by the agent that
        spoke it with its `SpeechHandle.chat_items`, so nothing here depends on the wording.

        Idempotent and empty-safe: an interrupted `say` can forward no text and so add no item, in
        which case `_ensure_anchor`'s first-handoff fallback takes over."""
        if self._anchor_items or not items:
            return
        self._anchor_items = [item for item in items if item.type == "message"]

    def _ensure_anchor(self, source: Agent) -> None:
        """Last-chance anchor capture, at the FIRST handoff only.

        Two gaps `note_opening_spoken` cannot cover: `ivr_agent.transfer_to_verification` carries
        with `carry_chat_ctx` directly and never reaches `prepare_successor`, so the opening can
        arrive INHERITED; and an interrupted say adds no chat item. Both leave the opening as the
        earliest assistant message in the source's context — positional and id-preserving, not a
        text match. Restricted to the first handoff so a later assistant turn (a gap re-ask, say)
        can never be mistaken for the opening.

        The rep's reply comes along when it directly follows: a pinned exchange reads as answered,
        where a lone opening question invites the closer to re-ask it."""
        if self._anchor_items or self._boundaries:
            return
        items = list(source.chat_ctx.items)
        index = next((i for i, item in enumerate(items) if _is_message(item, "assistant")), None)
        if index is None:
            return
        anchor = [items[index]]
        following = items[index + 1 : index + 2]
        if following and _is_message(following[0], "user"):
            anchor += following
        self._anchor_items = anchor

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

    def attach_observer(self, manager: "ObserverManager") -> None:
        """Hand the controller the Observer so `drain_observer` has something to await.
        Wired from main.py alongside `attach_session`; left unset in Voice Lab and tests."""
        self._observer_manager = manager

    def update_answers(self, answers: Mapping[str, Any]) -> None:
        """The answers the CALL has collected, laid over the pre-call baseline.

        MERGED, never replaced. The Observer's own map is not role-scoped, so a wholesale
        replace would put every ask-role intake value back and re-arm the question deletion
        `gating_seed` exists to prevent — and it would do so invisibly, since the controller
        cannot tell a pre-call value from one the rep just gave."""
        self._answers = {**self._baseline, **answers}

    async def drain_observer(self) -> None:
        """Let extraction settle before a caller reads `gap_fields`. No-op without a manager."""
        if self._observer_manager is not None:
            await self._observer_manager.drain_pending()

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

    def applicable_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """A task's questions whose gates hold, so they are askable on THIS call."""
        return self._classify_fields(task_index)[0]

    def excluded_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """Questions a gate rules out decidably — some gate is false with every path it
        reads already answered, so no later answer can turn it back on."""
        return self._classify_fields(task_index)[1]

    def conditional_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """Questions whose gates are not yet decidable — a referenced answer is still
        missing, so the compiled prompt's own condition governs them."""
        return self._classify_fields(task_index)[2]

    def _classify_fields(
        self, task_index: int
    ) -> tuple[list[PlanFieldDescriptor], list[PlanFieldDescriptor], list[PlanFieldDescriptor]]:
        """One pass over a task's fields into (applicable, excluded, conditional) — the
        partition `applicable_fields`/`excluded_fields`/`conditional_fields` share, so each
        field's gates are evaluated once per call instead of once per accessor."""
        shared = self.plan.shared_conditions
        applicable: list[PlanFieldDescriptor] = []
        excluded: list[PlanFieldDescriptor] = []
        conditional: list[PlanFieldDescriptor] = []
        for field in self.plan.tasks[task_index].fields:
            if is_applicable(field.gates, self._answers, shared):
                applicable.append(field)
            elif self._has_decided_false_gate(field, task_index):
                excluded.append(field)
            else:
                conditional.append(field)
        return applicable, excluded, conditional

    def _settled(self, path: str, task_index: int) -> bool:
        """Is this path's value final for the purposes of a gate evaluated at `task_index`?

        Answered, or collected by an EARLIER task — an earlier task's unanswered field was
        gated out upstream and no later answer is coming. Without the second half, a gate
        over 27 prior-auth paths on a call where infertility was never covered reads as
        undecided forever and the auth-department questions are asked anyway.

        A path NO task collects is deliberately not settled by position: an absent context
        value means "not supplied", which is unknown, not false.

        Position alone IS the compiler's rule (`question_plan._entry_decided`), which runs at
        dispatch with no answers; adding "answered" only makes this MORE decisive, never less.
        That ordering is required, not incidental: the compiler omits the prose for every gate
        it decides, so a worker less decisive than the compiler keeps a question whose
        condition is stated nowhere."""
        owner = self._task_of_path.get(path)
        return self._is_answered(path) or (owner is not None and owner < task_index)

    def _decided_true(
        self, cond: Condition, shared: Mapping[str, Condition], task_index: int
    ) -> bool:
        """Whether `cond` is decidably TRUE — recurses through `all`/`any`/`ref`/`not` the same
        way `evaluate` does, rather than flatly requiring every path anywhere in the tree
        answered (see `_decided_false`, which this and `_has_decided_false_gate` mirror)."""
        if isinstance(cond, RefCondition):
            target = shared.get(cond.ref)
            return self._decided_true(target, shared, task_index) if target is not None else False
        if isinstance(cond, AllCondition):
            return all(self._decided_true(c, shared, task_index) for c in cond.all)
        if isinstance(cond, AnyCondition):
            return any(self._decided_true(c, shared, task_index) for c in cond.any)
        if isinstance(cond, NotCondition):
            return self._decided_false(cond.not_, shared, task_index)
        return self._settled(cond.field, task_index) and evaluate(cond, self._answers, shared)

    def _decided_false(
        self, cond: Condition, shared: Mapping[str, Condition], task_index: int
    ) -> bool:
        """Whether `cond` is decidably FALSE. An `all` is decided-false the moment ONE
        conjunct is decided-false — mirroring `evaluate`'s `all(...)` short-circuit — so a
        sibling conjunct reading a still-unanswered path never blocks the decision. Flattening
        every path referenced anywhere in the tree (the prior bug) required `male_partner_in_scope`
        `AllCondition(family_coverage, spouse_gender == "Male")` to have `spouse_gender` answered
        before ever excluding it, even though `family_coverage` alone already decides it false."""
        if isinstance(cond, RefCondition):
            target = shared.get(cond.ref)
            return self._decided_false(target, shared, task_index) if target is not None else True
        if isinstance(cond, AllCondition):
            return any(self._decided_false(c, shared, task_index) for c in cond.all)
        if isinstance(cond, AnyCondition):
            return all(self._decided_false(c, shared, task_index) for c in cond.any)
        if isinstance(cond, NotCondition):
            return self._decided_true(cond.not_, shared, task_index)
        return self._settled(cond.field, task_index) and not evaluate(cond, self._answers, shared)

    def _has_decided_false_gate(self, field: PlanFieldDescriptor, task_index: int) -> bool:
        """`is_applicable` is `all(gates)`, so ONE decidably-false gate settles the
        whole chain — regardless of other gates reading unanswered paths."""
        shared = self.plan.shared_conditions
        return any(self._decided_false(gate, shared, task_index) for gate in field.gates)

    def gap_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """The unanswered applicable descriptors under this task's still-owed questions.

        Field-granular by design (Plan C, 2026-08-07: ceilings count asks, lists name
        missing fields) — re-asking a partially answered fan-out by its question text would
        re-ask the half already on file. Consults `self._alternatives`, never `default`: an
        either/or is one tree question, but `is_required` is unconditional on both its
        members, so answering one side leaves the sibling's OWN required-and-unanswered
        check true unless the group is checked here too."""
        task = self.plan.tasks[task_index]
        shared = self.plan.shared_conditions
        by_path = {field.path: field for field in task.fields}

        def answered(path: str) -> bool:
            return has_value(self._answers, path) or any(
                has_value(self._answers, sibling) for sibling in self._alternatives.get(path, ())
            )

        return [
            field
            for question in owed_now(task, self._answers, shared)
            for path in question.target_paths
            if (field := by_path.get(path)) is not None
            and is_applicable(field.gates, self._answers, shared)
            and is_required(field, self._answers, shared)
            and not answered(path)
        ]

    def owed_question_count(self, task_index: int) -> int:
        """`gap_fields` measured in SPOKEN questions — the ceiling both guards judge by.

        Unlike `gap_fields` this does not consult `self._alternatives`, so a fully-satisfied
        either/or still counts its ask; the ceiling errs high, which only makes the guards
        more patient."""
        return len(
            owed_now(self.plan.tasks[task_index], self._answers, self.plan.shared_conditions)
        )

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

    def _closing_agent(self) -> Agent:
        return self._agent_at(self._next_applicable(self._closing_task_index))

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
