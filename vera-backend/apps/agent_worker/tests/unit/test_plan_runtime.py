"""Plan runtime — one PlanTaskAgent per CallPlan task, sequential tool handoff,
skip-scan on applicable_when, wrap-up, and the agent-owned cursor write."""

import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import chat_ctx_texts, ctx_texts
from livekit.agents import Agent
from livekit.agents.llm import ChatMessage, FunctionTool

from agent_worker.directives import ReAsk, SkipToTask, Terminate
from agent_worker.handoff import carry_chat_ctx
from agent_worker.intervention import TakeoverState, push_coaching_note
from agent_worker.plan_runtime import (
    _OPENING_DIRECTIVE,
    _WRAP_UP_DIRECTIVE,
    WRAP_UP_TASK_KEY,
    GapTaskAgent,
    PlanRunController,
    PlanTaskAgent,
    WrapUpAgent,
)
from vera_core.forms.call_plan import CallPlan, PlanFieldDescriptor, PlanSession, PlanTask
from vera_core.forms.dsl import Comparison, RequiredWhen

ROOM = "call--t--c"


class FakeRunState:
    """Records cursor writes; `fail=True` simulates a Redis outage."""

    def __init__(self) -> None:
        self.cursor_writes: list[tuple[str, str]] = []
        self.fail = False

    async def set_active_task(self, room_name: str, task_key: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.cursor_writes.append((room_name, task_key))


def _plan() -> CallPlan:
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P text.", goal="G text.", base_instructions="B text."),
        tasks=[
            PlanTask(
                task_key="intro_task",
                title="Introduction",
                intro="Hello rep.",
                outro="Moving on.",
                prompt="Do the intro things.",
            ),
            PlanTask(
                task_key="gated_task",
                title="Gated",
                prompt="Only when in network.",
                applicable_when=Comparison(field="sections.a.in_network", op="eq", value="Yes"),
            ),
            PlanTask(task_key="last_task", title="Last", intro="Next up.", prompt="Finish up."),
        ],
    )


def _controller(
    plan: CallPlan | None = None,
    run_state: FakeRunState | None = None,
    *,
    greeting: str | None = None,
    extra_instructions: str | None = None,
    gap_pass_enabled: bool = True,
    previous_task_only: bool = True,
) -> tuple[PlanRunController, FakeRunState]:
    state = run_state or FakeRunState()
    controller = PlanRunController(
        plan or _plan(),
        room_name=ROOM,
        run_state=cast(Any, state),
        greeting=greeting,
        extra_instructions=extra_instructions,
        gap_pass_enabled=gap_pass_enabled,
        previous_task_only=previous_task_only,
    )
    return controller, state


def _tool(agent: Agent, name: str) -> Callable[[], Awaitable[Any]]:
    """The named tool, pre-bound with a `reason` — every tool requires one, and no test here
    cares what it says (the reason is transcript evidence, and nothing in the runtime reads it)."""
    tool = next(t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == name)
    return functools.partial(tool, reason="the task's questions are all answered")


def _session_patch(agent: Agent, mock_session: MagicMock) -> Any:
    # A real latch: a bare MagicMock attribute reads truthy and trips the takeover guards.
    mock_session.userdata = TakeoverState()
    # on_enter awaits the intro's playout before leading into the task.
    mock_session.say.return_value.wait_for_playout = AsyncMock()
    # on_enter pins the opening from the say's chat items; a bare MagicMock is not iterable.
    mock_session.say.return_value.chat_items = []
    return patch.object(type(agent), "session", new=property(lambda self: mock_session))


class TestConstruction:
    def test_one_agent_per_task_all_built_up_front(self) -> None:
        controller, _ = _controller()
        assert len(controller.agents) == 3
        assert all(isinstance(a, PlanTaskAgent) for a in controller.agents)
        assert isinstance(controller.wrap_up_agent, WrapUpAgent)
        assert controller.first_agent() is controller.agents[0]

    def test_instructions_fuse_session_and_task(self) -> None:
        controller, _ = _controller()
        instructions = controller.agents[0].instructions
        assert "P text." in instructions
        assert "G text." in instructions
        assert "B text." in instructions
        assert "Introduction" in instructions
        assert "Do the intro things." in instructions
        # only its OWN task's prompt — never a sibling's
        assert "Only when in network." not in instructions
        # the Cartesia TTS markup guide is appended (compiled prompts carry no
        # <spell> guidance, so plan-only must add it here)
        assert "<spell>" in instructions

    def test_scope_discipline_appended_to_every_agent(self) -> None:
        # The off-script guardrail must reach every plan agent (and wrap-up), so the
        # LLM never invents questions outside the compiled task list.
        controller, _ = _controller()
        for agent in [*controller.agents, controller.wrap_up_agent]:
            assert "only the questions listed" in agent.instructions.lower()

    def test_closing_discipline_appended_to_every_agent(self) -> None:
        # A task's authored outro IS the closing line; a farewell the model adds itself lands
        # immediately before it and signs the call off twice.
        controller, _ = _controller()
        for agent in [*controller.agents, controller.wrap_up_agent]:
            assert "never say goodbye" in agent.instructions.lower()

    def test_extra_instructions_overlay_every_agent(self) -> None:
        controller, _ = _controller(extra_instructions="Confirm the member ID twice.")
        for agent in [*controller.agents, controller.wrap_up_agent]:
            assert "Confirm the member ID twice." in agent.instructions

    def test_no_extra_instructions_means_no_overlay(self) -> None:
        controller, _ = _controller()
        assert "Additional instructions" not in controller.agents[0].instructions

    def test_on_file_values_rendered_into_instructions(self) -> None:
        plan = _plan().model_copy(update={"on_file_values": "Policy / Member ID: ABC123"})
        controller, _ = _controller(plan)
        for agent in [*controller.agents, controller.wrap_up_agent]:
            assert "already on file" in agent.instructions.lower()
            assert "Policy / Member ID: ABC123" in agent.instructions

    def test_no_on_file_values_means_no_block(self) -> None:
        controller, _ = _controller()
        assert "already on file" not in controller.agents[0].instructions.lower()

    def test_task_agent_is_dialogue_only(self) -> None:
        controller, _ = _controller()
        names = [t.info.name for t in controller.agents[0].tools if isinstance(t, FunctionTool)]
        assert names == ["task_complete"]  # no answer-write tools, no end_call

    def test_task_agents_get_their_schema_task_key_as_id(self) -> None:
        controller, _ = _controller()
        assert [a.id for a in controller.agents] == ["intro_task", "gated_task", "last_task"]

    def test_wrap_up_agent_id_is_the_sentinel(self) -> None:
        controller, _ = _controller()
        assert controller.wrap_up_agent.id == WRAP_UP_TASK_KEY


class TestHandoff:
    @pytest.mark.asyncio
    async def test_task_complete_tags_the_handoff_span(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.agents[0]
        tracer = trace.get_tracer("test")
        controller.update_answers({"sections.a.in_network": "Yes"})
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await _tool(agent, "task_complete")()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.from_task"] == "intro_task"
        assert span.attributes["vera.handoff.to_task"] == "gated_task"
        assert span.attributes["vera.handoff.reason"] == "task_complete"

    @pytest.mark.asyncio
    async def test_task_complete_to_wrap_up_tags_the_sentinel(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.agents[2]  # last_task
        tracer = trace.get_tracer("test")
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await _tool(agent, "task_complete")()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.to_task"] == WRAP_UP_TASK_KEY

    @pytest.mark.asyncio
    async def test_task_complete_returns_the_next_agent(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[0]
        mock_session = MagicMock()
        controller.update_answers({"sections.a.in_network": "Yes"})
        with _session_patch(agent, mock_session):
            handoff = await _tool(agent, "task_complete")()
        assert handoff is controller.agents[1]

    @pytest.mark.asyncio
    async def test_outro_is_spoken_before_the_handoff(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[0]
        mock_session = MagicMock()
        controller.update_answers({"sections.a.in_network": "Yes"})
        with _session_patch(agent, mock_session):
            await _tool(agent, "task_complete")()
        mock_session.say.assert_called_once_with("Moving on.")

    @pytest.mark.asyncio
    async def test_no_outro_no_exit_speech(self) -> None:
        # Outro absent → nothing spoken on exit (symmetric to the no-intro entry case).
        controller, _ = _controller()
        agent = controller.agents[2]  # last_task has no outro
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await _tool(agent, "task_complete")()
        mock_session.say.assert_not_called()

    @pytest.mark.asyncio
    async def test_blank_outro_no_exit_speech(self) -> None:
        # A prompt-document override may be "" — an operator's explicit "say nothing".
        plan = _plan()
        plan.tasks[0] = plan.tasks[0].model_copy(update={"outro": ""})
        controller, _ = _controller(plan)
        agent = controller.agents[0]
        mock_session = MagicMock()
        controller.update_answers({"sections.a.in_network": "Yes"})
        with _session_patch(agent, mock_session):
            await _tool(agent, "task_complete")()
        mock_session.say.assert_not_called()

    @pytest.mark.asyncio
    async def test_inapplicable_task_is_skipped(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[0]
        # gated_task requires in_network == "Yes"; unanswered ("") → skip to last_task
        with _session_patch(agent, MagicMock()):
            handoff = await _tool(agent, "task_complete")()
        assert handoff is controller.agents[2]

    @pytest.mark.asyncio
    async def test_applicability_falls_back_to_prefill_without_the_observer(self) -> None:
        """Regression fence for the per-tenant AI form-filling switch: with the Observer
        off nothing calls `update_answers`, so `_answers` stays at the intake prefill and
        `applicable_when` routes off THAT, not off what the rep says. Deliberate — keeping
        answers current requires extraction, which is exactly what the switch disables —
        and stated in the platform-settings copy. See agent_worker/main.py's gate."""
        prefilled = _plan().model_copy(update={"prefilled": {"sections.a.in_network": "Yes"}})
        controller, _ = _controller(prefilled)
        agent = controller.agents[0]
        # No update_answers call anywhere: the prefill alone opens the gated task, where an
        # unprefilled plan would have skipped it (test_inapplicable_task_is_skipped).
        with _session_patch(agent, MagicMock()):
            handoff = await _tool(agent, "task_complete")()
        assert handoff is controller.agents[1]

    @pytest.mark.asyncio
    async def test_final_task_hands_off_to_wrap_up(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[2]
        with _session_patch(agent, MagicMock()):
            handoff = await _tool(agent, "task_complete")()
        assert handoff is controller.wrap_up_agent

    @pytest.mark.asyncio
    async def test_task_complete_carries_conversation_into_the_successor(self) -> None:
        # The successor must remember the call so far — otherwise it re-greets and
        # re-asks answered questions (the transcript's re-introduction bug).
        controller, _ = _controller()
        agent = controller.agents[0]
        agent._chat_ctx.add_message(role="assistant", content="Hello, this is VERA.")
        agent._chat_ctx.add_message(role="user", content="The plan is PPO.")
        with _session_patch(agent, MagicMock()):
            successor = await _tool(agent, "task_complete")()
        texts = chat_ctx_texts(successor)
        assert "Hello, this is VERA." in texts
        assert "The plan is PPO." in texts


class TestOnEnter:
    @pytest.mark.asyncio
    async def test_on_enter_tags_the_current_span_with_task_identity(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.agents[1]  # gated_task, index 1
        tracer = trace.get_tracer("test")
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.task.key"] == "gated_task"
        assert span.attributes["vera.task.index"] == 1

    @pytest.mark.asyncio
    async def test_wrap_up_on_enter_tags_the_sentinel(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.wrap_up_agent
        tracer = trace.get_tracer("test")
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.task.key"] == WRAP_UP_TASK_KEY
        assert "vera.task.index" not in span.attributes

    @pytest.mark.asyncio
    async def test_says_intro_and_writes_cursor(self) -> None:
        controller, state = _controller()
        agent = controller.agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        mock_session.say.assert_called_once_with("Hello rep.")
        # The call's opening turn belongs to the rep: greet, then wait for them to
        # answer — never greet and fire a question in the same breath.
        mock_session.generate_reply.assert_not_called()
        assert state.cursor_writes == [(ROOM, "intro_task")]

    @pytest.mark.asyncio
    async def test_first_task_greeting_overrides_intro(self) -> None:
        controller, _ = _controller(greeting="Custom greeting.")
        agent = controller.agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        mock_session.say.assert_called_once_with("Custom greeting.")
        # Same as any opener: greeting only, then the rep's turn.
        mock_session.generate_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_opening_turn_without_intro_is_silent(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[1]  # gated_task has no intro
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        # No intro on the opening turn → nothing at all; the rep speaks first.
        mock_session.say.assert_not_called()
        mock_session.generate_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_transition_with_intro_leads_into_the_task(self) -> None:
        controller, _ = _controller()
        opener, agent = controller.agents[0], controller.agents[2]
        with _session_patch(opener, MagicMock()):
            await opener.on_enter()  # consumes the call's opening turn
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        mock_session.say.assert_called_once_with("Next up.")
        # Mid-call swap: the intro alone would leave the rep waiting.
        mock_session.generate_reply.assert_called_once()

    @pytest.mark.asyncio
    async def test_transition_without_intro_still_leads(self) -> None:
        # A task with no intro is exactly where dead air is most likely, so the lead
        # is gated on the transition — not on the intro being present.
        controller, _ = _controller()
        opener, agent = controller.agents[0], controller.agents[1]
        with _session_patch(opener, MagicMock()):
            await opener.on_enter()
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        mock_session.say.assert_not_called()
        mock_session.generate_reply.assert_called_once()

    @pytest.mark.asyncio
    async def test_intro_is_spoken_before_the_lead(self) -> None:
        # The lead must never be queued on top of in-flight TTS, or the intro gets
        # cut off — assert the playout was awaited first.
        controller, _ = _controller()
        opener, agent = controller.agents[0], controller.agents[2]
        with _session_patch(opener, MagicMock()):
            await opener.on_enter()
        order: list[str] = []
        mock_session = MagicMock()
        mock_session.generate_reply.side_effect = lambda **_: order.append("generate_reply")
        with _session_patch(agent, mock_session):
            mock_session.say.return_value.wait_for_playout = AsyncMock(
                side_effect=lambda: order.append("playout")
            )
            await agent.on_enter()
            await controller.drain_cursor_writes()
        assert order == ["playout", "generate_reply"]

    @pytest.mark.asyncio
    async def test_on_enter_under_takeover_is_silent(self) -> None:
        # A task swap landing mid-takeover must not speak — the intro is disruptive
        # enough, an LLM-generated question more so.
        controller, _ = _controller()
        agent = controller.agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            mock_session.userdata.engaged = True
            await agent.on_enter()
            await controller.drain_cursor_writes()
        mock_session.say.assert_not_called()
        mock_session.generate_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_cursor_write_failure_never_blocks_speech(self) -> None:
        state = FakeRunState()
        state.fail = True
        controller, _ = _controller(run_state=state)
        agent = controller.agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()  # must not raise
            await controller.drain_cursor_writes()
        mock_session.say.assert_called_once_with("Hello rep.")

    @pytest.mark.asyncio
    async def test_cursor_tracks_the_active_agent_in_process(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[2]
        with _session_patch(agent, MagicMock()):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        assert controller.active_task_index == 2


class TestWrapUp:
    @pytest.mark.asyncio
    async def test_wrap_up_writes_cursor_and_prompts_the_close(self) -> None:
        controller, state = _controller()
        agent = controller.wrap_up_agent
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        assert state.cursor_writes == [(ROOM, WRAP_UP_TASK_KEY)]
        mock_session.generate_reply.assert_called_once()

    @staticmethod
    def _directive(mock_session: MagicMock) -> str:
        return cast(str, mock_session.generate_reply.call_args.kwargs["instructions"])

    async def _enter_wrap_up(self, controller: PlanRunController) -> MagicMock:
        """Finish the closing task the way the chain does, then enter wrap-up."""
        closing = controller.agents[-1]
        with _session_patch(closing, MagicMock()):
            successor = cast(Agent, await _tool(closing, "task_complete")())
        assert successor is controller.wrap_up_agent
        mock_session = MagicMock()
        with _session_patch(controller.wrap_up_agent, mock_session):
            await controller.wrap_up_agent.on_enter()
        return mock_session

    @pytest.mark.asyncio
    async def test_wrap_up_hangs_up_itself_after_the_closing_outro(self) -> None:
        # The outro IS the goodbye, spoken verbatim just before the swap, so wrap-up must not
        # produce a turn at all — asking the LLM to close silently was obeyed in only two of
        # three eval scenarios, and the third spoke a second goodbye.
        plan = _plan()
        plan.tasks[-1] = plan.tasks[-1].model_copy(update={"outro": "Have a wonderful day!"})
        controller, _ = _controller(plan)
        controller.update_answers({"sections.a.in_network": "Yes"})
        mock_session = await self._enter_wrap_up(controller)
        mock_session.generate_reply.assert_not_called()
        mock_session.shutdown.assert_called_once_with(drain=True)

    @pytest.mark.asyncio
    async def test_wrap_up_says_goodbye_when_the_closing_task_has_no_outro(self) -> None:
        # An outro is authored per-schema (`disease_only`'s wrap_up has none), so silencing
        # wrap-up unconditionally would hang up with no closing line at all.
        controller, _ = _controller()  # `_plan`'s last task authors no outro
        controller.update_answers({"sections.a.in_network": "Yes"})
        mock_session = await self._enter_wrap_up(controller)
        assert self._directive(mock_session) == _WRAP_UP_DIRECTIVE
        mock_session.shutdown.assert_not_called()  # the goodbye has to be spoken first

    @pytest.mark.asyncio
    async def test_an_earlier_outro_does_not_silence_the_close(self) -> None:
        # Every task speaks an outro, so the flag must be ASSIGNED per task, not accumulated:
        # `_plan`'s intro_task has one and its closing task does not.
        controller, _ = _controller()
        controller.update_answers({"sections.a.in_network": "Yes"})
        opener = controller.agents[0]
        with _session_patch(opener, MagicMock()):
            await _tool(opener, "task_complete")()
        assert controller.signed_off, "the opener's outro was not recorded"
        assert self._directive(await self._enter_wrap_up(controller)) == _WRAP_UP_DIRECTIVE

    @pytest.mark.asyncio
    async def test_a_terminate_directive_still_gets_a_spoken_goodbye(self) -> None:
        # `apply_directive_now` swaps straight here, so no outro ever played — an inactive-policy
        # call must not hang up wordlessly.
        controller, _ = _controller()
        controller.note_task_entered(0)
        _attach_ordered_session(controller)
        await controller.apply_directive_now(Terminate(rule_key="insurance_not_active"))
        assert not controller.signed_off
        mock_session = MagicMock()
        with _session_patch(controller.wrap_up_agent, mock_session):
            await controller.wrap_up_agent.on_enter()
        assert self._directive(mock_session) == _WRAP_UP_DIRECTIVE
        mock_session.shutdown.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrap_up_stays_silent_under_takeover(self) -> None:
        controller, _ = _controller()
        mock_session = MagicMock()
        with _session_patch(controller.wrap_up_agent, mock_session):
            mock_session.userdata.engaged = True
            await controller.wrap_up_agent.on_enter()
        mock_session.generate_reply.assert_not_called()
        mock_session.shutdown.assert_not_called()  # a supervisor's call is theirs to end

    @pytest.mark.asyncio
    async def test_wrap_up_end_call_shuts_down_and_returns_nothing(self) -> None:
        # None on purpose: tool OUTPUT sets reply_required, and LiveKit schedules that follow-up
        # with force=True, bypassing the drain — so a returned string is spoken to the rep as one
        # last line ("I have successfully concluded the call.").
        controller, _ = _controller()
        agent = controller.wrap_up_agent
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            result = await _tool(agent, "end_call")()
        assert result is None
        mock_session.shutdown.assert_called_once_with(drain=True)

    @pytest.mark.asyncio
    async def test_end_call_under_takeover_refuses_in_words(self) -> None:
        # The one case where a spoken reply IS wanted: the return text tells the model to stop.
        controller, _ = _controller()
        agent = controller.wrap_up_agent
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            mock_session.userdata.engaged = True
            result = await _tool(agent, "end_call")()
        assert isinstance(result, str)
        assert "supervisor" in result
        mock_session.shutdown.assert_not_called()


def _attach_ordered_session(controller: PlanRunController) -> tuple[MagicMock, list[Any]]:
    """A mock session that records interrupt/swap/reply call order, so a test can assert the
    bot was interrupted (went silent) BEFORE the redirect."""
    order: list[Any] = []
    session = MagicMock()
    session.userdata.engaged = False  # no supervisor takeover
    session.interrupt = AsyncMock(side_effect=lambda: order.append("interrupt"))
    session.update_agent = MagicMock(side_effect=lambda a: order.append(("update_agent", a)))
    session.generate_reply = MagicMock(side_effect=lambda **k: order.append(("generate_reply", k)))
    controller.attach_session(session)
    return session, order


class TestDirectiveIntervention:
    @pytest.mark.asyncio
    async def test_handoffs_serialize_on_the_controller_lock(self) -> None:
        controller, _ = _controller()
        async with controller.lock:
            # a concurrent task_complete must wait for the lock, not double-swap
            task = asyncio.create_task(controller.advance_from(0))
            await asyncio.sleep(0)
            assert not task.done()
        assert await task is controller.agents[2]

    @pytest.mark.asyncio
    async def test_directive_without_an_attached_session_is_a_silent_noop(self) -> None:
        """With the AI form-filling switch off the worker skips `attach_session`, so this
        path is live in production. It must degrade quietly, not raise — self-consistent
        today because directives only ever come from the Observer's own rule engine, which
        is off in the same breath."""
        controller, _ = _controller()
        controller.note_task_entered(0)
        await controller.apply_directive_now(Terminate(rule_key="not_covered"))
        assert controller.active_task_index == 0  # no swap happened

    @pytest.mark.asyncio
    async def test_terminate_interrupts_then_swaps_to_wrap_up(self) -> None:
        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, order = _attach_ordered_session(controller)
        await controller.apply_directive_now(Terminate(rule_key="not_covered"))
        # bot silenced (interrupt) BEFORE the swap
        assert order == ["interrupt", ("update_agent", controller.wrap_up_agent)]

    @pytest.mark.asyncio
    async def test_skip_forward_interrupts_then_swaps(self) -> None:
        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, order = _attach_ordered_session(controller)
        await controller.apply_directive_now(SkipToTask(rule_key="jump", task_key="last_task"))
        assert order == ["interrupt", ("update_agent", controller.agents[2])]

    @pytest.mark.asyncio
    async def test_skip_to_current_or_behind_is_a_noop(self) -> None:
        controller, _ = _controller()
        controller.note_task_entered(2)  # already at last_task
        session, order = _attach_ordered_session(controller)
        await controller.apply_directive_now(SkipToTask(rule_key="back", task_key="intro_task"))
        assert order == []  # no interrupt, no swap
        session.update_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_reask_interrupts_then_generates_reply(self) -> None:
        controller, _ = _controller()
        controller.note_task_entered(0)
        session, order = _attach_ordered_session(controller)
        await controller.apply_directive_now(
            ReAsk(rule_key="ded", reason="Deductible was stated twice.", clarify="Which is right?")
        )
        assert order[0] == "interrupt"
        assert order[1][0] == "generate_reply"
        instructions = order[1][1]["instructions"]
        assert "CONSISTENCY CHECK" in instructions and "Which is right?" in instructions
        session.update_agent.assert_not_called()  # re-ask keeps the same agent

    @pytest.mark.asyncio
    async def test_no_session_attached_is_a_noop(self) -> None:
        controller, _ = _controller()
        controller.note_task_entered(0)
        await controller.apply_directive_now(Terminate(rule_key="t"))  # must not raise

    @pytest.mark.asyncio
    async def test_apply_failure_is_swallowed(self) -> None:
        controller, _ = _controller()
        controller.note_task_entered(0)
        session = MagicMock()
        session.userdata.engaged = False
        session.interrupt = AsyncMock(side_effect=RuntimeError("boom"))
        controller.attach_session(session)
        # a redirect failure must never bubble into the Observer / drop the call
        await controller.apply_directive_now(Terminate(rule_key="t"))

    @pytest.mark.asyncio
    async def test_no_directive_fires_after_wrap_up_entered(self) -> None:
        # A rule fired late (the outgoing observer's final drain) must not re-enter
        # wrap-up (double goodbye), yank the call back into a task, or interrupt the
        # goodbye with a re-ask — once no task is active, every directive is a no-op.
        controller, _ = _controller()
        controller.note_wrap_up_entered()
        session, order = _attach_ordered_session(controller)
        await controller.apply_directive_now(SkipToTask(rule_key="late", task_key="intro_task"))
        await controller.apply_directive_now(Terminate(rule_key="late"))
        await controller.apply_directive_now(ReAsk(rule_key="late", reason="x"))
        assert order == []  # no interrupt, no swap, no reply
        session.generate_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_op_while_supervisor_has_taken_over(self) -> None:
        # Under a live human takeover the rule engine must not yank the agent around.
        controller, _ = _controller()
        controller.note_task_entered(0)
        session, order = _attach_ordered_session(controller)
        session.userdata.engaged = True  # supervisor is driving the call
        await controller.apply_directive_now(Terminate(rule_key="t"))
        assert order == []  # no interrupt, no swap

    @pytest.mark.asyncio
    async def test_terminate_tags_the_ambient_span_with_handoff_attrs(
        self, otel_spans: Any
    ) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, _order = _attach_ordered_session(controller)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("probe"):
            await controller.apply_directive_now(Terminate(rule_key="not_covered"))
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.from_task"] == "intro_task"
        assert span.attributes["vera.handoff.to_task"] == WRAP_UP_TASK_KEY
        assert span.attributes["vera.handoff.reason"] == "flow_rule"

    @pytest.mark.asyncio
    async def test_skip_forward_tags_the_ambient_span(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, _order = _attach_ordered_session(controller)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("probe"):
            await controller.apply_directive_now(SkipToTask(rule_key="jump", task_key="last_task"))
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.to_task"] == "last_task"

    @pytest.mark.asyncio
    async def test_reask_does_not_tag_handoff_attrs(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, _order = _attach_ordered_session(controller)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("probe"):
            await controller.apply_directive_now(
                ReAsk(rule_key="ded", reason="Deductible was stated twice.")
            )
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert "vera.handoff.from_task" not in span.attributes


class TestPrefill:
    @pytest.mark.asyncio
    async def test_prefilled_answers_gate_tasks_from_call_start(self) -> None:
        # gated_task requires in_network == "Yes" — satisfied by intake prefill,
        # with no Observer write needed.
        plan = _plan().model_copy(update={"prefilled": {"sections.a.in_network": "Yes"}})
        controller, _ = _controller(plan)
        agent = controller.agents[0]
        with _session_patch(agent, MagicMock()):
            handoff = await _tool(agent, "task_complete")()
        assert handoff is controller.agents[1]

    def test_known_information_block_fused_into_instructions(self) -> None:
        plan = _plan().model_copy(update={"known_information": "Patient Name: Jane Doe"})
        controller, _ = _controller(plan)
        for agent in [*controller.agents, controller.wrap_up_agent]:
            assert "# Known information" in agent.instructions
            assert "Patient Name: Jane Doe" in agent.instructions

    def test_no_known_information_means_no_block(self) -> None:
        controller, _ = _controller()
        assert "# Known information" not in controller.agents[0].instructions


def _field(
    path: str,
    title: str,
    *,
    required: bool | RequiredWhen = True,
    gates: tuple[Comparison, ...] = (),
    values: list[str] | None = None,
) -> PlanFieldDescriptor:
    return PlanFieldDescriptor(
        path=path,
        title=title,
        type="text",
        role="ask",
        required=required,
        gates=gates,
        values=values,
    )


def _gap_plan() -> CallPlan:
    """Four tasks; the LAST (`closing_task`) is the closer — like the DSL's `wrap_up` task
    that collects the reference number and says goodbye. The gap pass sweeps the three
    substantive tasks BEFORE it. `gated_task` is only applicable when in_network == "Yes";
    `coverage_task.oon_note` gates the opposite way."""
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(
                task_key="intro_task",
                title="Introduction",
                intro="Hello rep.",
                prompt="Intro.",
                fields=[
                    _field("sections.intro.rep_name", "Representative name"),
                    _field("sections.intro.notes", "Notes", required=False),
                ],
            ),
            PlanTask(
                task_key="gated_task",
                title="Gated",
                prompt="In network only.",
                applicable_when=Comparison(field="sections.a.in_network", op="eq", value="Yes"),
                fields=[_field("sections.gated.copay", "Copay")],
            ),
            PlanTask(
                task_key="coverage_task",
                title="Coverage",
                prompt="Coverage details.",
                fields=[
                    _field("sections.cov.deductible", "Deductible", values=["Met", "Not met"]),
                    _field(
                        "sections.cov.oon_note",
                        "OON note",
                        gates=(Comparison(field="sections.a.in_network", op="eq", value="No"),),
                    ),
                ],
            ),
            PlanTask(
                task_key="closing_task",
                title="Wrap Up",
                prompt="Collect the reference number, then end the call.",
                outro="have a wonderful day!",
                fields=[_field("sections.close.ref_number", "Reference number")],
            ),
        ],
    )


_CLOSER = 3  # index of closing_task in _gap_plan()


class TestGating:
    """`coverage_task` is the shape that failed on a live eval: one applicable question
    (deductible) plus one the gates exclude (oon_note, gated on in_network == "No")."""

    async def _enter(self, controller: PlanRunController, index: int) -> Agent:
        agent = controller.agents[index]
        with _session_patch(agent, MagicMock()):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        return agent

    @pytest.mark.asyncio
    async def test_gating_lands_in_the_instructions_not_a_one_shot_directive(self) -> None:
        # The defect: the list was a generate_reply directive, which LiveKit staples to a COPY
        # of the chat ctx and discards — so from turn 2 the agent asked the excluded question.
        # Instructions persist for every turn of the task, which is the whole fix.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        agent = await self._enter(controller, 2)
        assert "OON note" in agent.instructions
        assert "Deductible" in agent.instructions

    @pytest.mark.asyncio
    async def test_the_applicable_questions_are_named_before_the_excluded_ones(self) -> None:
        # An exclusions-only list read as "the whole task is excluded" and the task completed
        # itself immediately. Leading with what DOES apply is what prevents that reading.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        agent = await self._enter(controller, 2)
        assert agent.instructions.index("Deductible") < agent.instructions.index("OON note")
        assert "apply on THIS call" in agent.instructions

    @pytest.mark.asyncio
    async def test_a_task_with_nothing_excluded_keeps_its_original_instructions(self) -> None:
        # No gates in play → no narrowing text at all, so the common case is untouched.
        controller, _ = _controller(_gap_plan())
        before = controller.agents[0].instructions
        agent = await self._enter(controller, 0)
        assert agent.instructions == before

    @pytest.mark.asyncio
    async def test_re_entry_re_narrows_instead_of_stacking(self) -> None:
        # A ReAsk directive re-enters the task; the block must be rebuilt against fresher
        # answers, not appended again.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        agent = await self._enter(controller, 2)
        once = agent.instructions
        await self._enter(controller, 2)
        assert agent.instructions == once


class TestPrematureCompletion:
    @pytest.mark.asyncio
    async def test_task_complete_is_refused_while_required_questions_are_open(self) -> None:
        controller, _ = _controller(_gap_plan())
        agent = controller.agents[0]  # intro_task: rep_name required + unanswered
        with _session_patch(agent, MagicMock()):
            result = await _tool(agent, "task_complete")()
        # A str parks the plan on this task; an Agent would have advanced it.
        assert isinstance(result, str)
        assert "Representative name" in result
        assert controller.agents[0] is agent  # cursor did not move on

    @pytest.mark.asyncio
    async def test_the_refusal_names_every_open_question(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        agent = controller.agents[2]
        with _session_patch(agent, MagicMock()):
            result = cast(str, await _tool(agent, "task_complete")())
        assert "Deductible" in result
        assert "OON note" not in result  # inapplicable, so never outstanding

    @pytest.mark.asyncio
    async def test_a_second_task_complete_advances_even_with_questions_still_open(self) -> None:
        # THE no-deadlock property. A rep who cannot answer never empties gap_fields, so an
        # unconditional guard would refuse every completion and strand the call on this task.
        controller, _ = _controller(_gap_plan())
        agent = controller.agents[0]
        with _session_patch(agent, MagicMock()):
            assert isinstance(await _tool(agent, "task_complete")(), str)
            second = await _tool(agent, "task_complete")()
        assert isinstance(second, Agent)
        assert second is not agent

    @pytest.mark.asyncio
    async def test_a_complete_task_is_never_refused(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.intro.rep_name": "Pat"})
        agent = controller.agents[0]
        with _session_patch(agent, MagicMock()):
            result = await _tool(agent, "task_complete")()
        assert isinstance(result, Agent)

    @pytest.mark.asyncio
    async def test_another_tasks_open_questions_do_not_block_this_one(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.intro.rep_name": "Pat"})  # task 0 satisfied
        agent = controller.agents[0]  # task 2's deductible is still open, and irrelevant here
        with _session_patch(agent, MagicMock()):
            assert isinstance(await _tool(agent, "task_complete")(), Agent)

    @pytest.mark.asyncio
    async def test_takeover_still_wins_over_the_guard(self) -> None:
        # Under takeover nothing may be spoken, so the refusal must not preempt that check.
        controller, _ = _controller(_gap_plan())
        agent = controller.agents[0]
        session = MagicMock()
        with _session_patch(agent, session):
            session.userdata.engaged = True
            result = cast(str, await _tool(agent, "task_complete")())
        assert "supervisor" in result


class TestGapDetection:
    def test_gap_fields_is_required_applicable_and_unanswered(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        # intro: rep_name required+unanswered → gap; notes optional → excluded
        assert [f.path for f in controller.gap_fields(0)] == ["sections.intro.rep_name"]

    def test_answered_field_is_not_a_gap(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.intro.rep_name": "Pat"})
        assert controller.gap_fields(0) == []

    def test_blank_answer_is_still_a_gap(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.intro.rep_name": "   "})
        assert [f.path for f in controller.gap_fields(0)] == ["sections.intro.rep_name"]

    def test_inapplicable_field_is_not_a_gap(self) -> None:
        # coverage_task.oon_note gates on in_network == "No"; with "Yes" it is inapplicable.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        assert [f.path for f in controller.gap_fields(2)] == ["sections.cov.deductible"]

    def test_conditional_required_resolves_against_live_answers(self) -> None:
        plan = _gap_plan()
        plan.tasks[0].fields[1] = _field(
            "sections.intro.notes",
            "Notes",
            required=RequiredWhen(when=Comparison(field="sections.a.vip", op="eq", value="Yes")),
        )
        controller, _ = _controller(plan)
        controller.update_answers({"sections.intro.rep_name": "Pat", "sections.a.vip": "Yes"})
        assert [f.path for f in controller.gap_fields(0)] == ["sections.intro.notes"]
        controller.update_answers({"sections.intro.rep_name": "Pat", "sections.a.vip": "No"})
        assert controller.gap_fields(0) == []


class TestGapRouting:
    """The gap pass runs when advancing INTO the closing task — so any re-ask lands before
    the closer collects the reference number and says goodbye, never after."""

    async def _advance_into_closer(self, controller: PlanRunController) -> Agent:
        # Walk the three substantive tasks, then complete the last substantive one — its
        # successor (_next_applicable) is the closer, which is the gap-pass trigger point.
        for i in (0, 1, 2):
            controller.note_task_entered(i)
        return await controller.advance_from(2)

    @pytest.mark.asyncio
    async def test_diverts_to_gap_agent_before_the_closer(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})  # rep_name still missing
        successor = await self._advance_into_closer(controller)
        assert successor is controller.gap_agents[0]  # gap pass, NOT the closer yet

    @pytest.mark.asyncio
    async def test_goes_straight_to_the_closer_when_no_gaps(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers(
            {
                "sections.a.in_network": "Yes",
                "sections.intro.rep_name": "Pat",
                "sections.gated.copay": "$20",
                "sections.cov.deductible": "Met",
            }
        )
        successor = await self._advance_into_closer(controller)
        assert successor is controller.agents[_CLOSER]  # closing task, no gap agent
        assert controller._gap_pass_done is True

    @pytest.mark.asyncio
    async def test_flag_off_goes_straight_to_the_closer_despite_gaps(self) -> None:
        controller, _ = _controller(_gap_plan(), gap_pass_enabled=False)
        successor = await self._advance_into_closer(controller)
        assert successor is controller.agents[_CLOSER]

    @pytest.mark.asyncio
    async def test_closer_turning_inapplicable_mid_pass_falls_through_to_wrap_up(self) -> None:
        # The pass exists BECAUSE the Observer keeps writing answers while it runs, so the
        # closer's applicable_when can flip between entering the pass and leaving it.
        plan = _gap_plan()
        plan.tasks[_CLOSER].applicable_when = Comparison(
            field="sections.a.in_network", op="eq", value="Yes"
        )
        controller, _ = _controller(plan)
        controller.update_answers({"sections.a.in_network": "Yes"})
        controller.note_task_entered(0)  # intro has a gap
        gap_agent = await controller.advance_from(2)
        assert gap_agent is controller.gap_agents[0]
        # mid-sweep the rep corrects in_network → the closer no longer applies
        controller.update_answers({"sections.a.in_network": "No", "sections.intro.rep_name": "Pat"})
        with _session_patch(gap_agent, MagicMock()):
            successor = await _tool(gap_agent, "gap_complete")()
        assert successor is controller.wrap_up_agent

    @pytest.mark.asyncio
    async def test_single_task_plan_never_sweeps(self) -> None:
        # Its closer IS index 0, and advance_from only ever asks for _next_applicable(>= 1).
        plan = _gap_plan()
        plan.tasks = [plan.tasks[0]]
        controller, _ = _controller(plan)
        assert controller.gap_agents == []
        controller.note_task_entered(0)
        successor = await controller.advance_from(0)
        assert successor is controller.wrap_up_agent

    @pytest.mark.asyncio
    async def test_closer_completion_goes_to_wrap_up_without_re_running(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller._gap_pass_done = True
        controller.note_task_entered(_CLOSER)
        successor = await controller.advance_from(_CLOSER)
        assert successor is controller.wrap_up_agent


class TestGapFlowRules:
    @pytest.mark.asyncio
    async def test_skip_to_task_bypassed_task_is_not_swept(self) -> None:
        # gated_task (index 1) is applicable (in_network == "Yes") and has an unanswered
        # required field, but a skip_to_task flow rule bypassed it — it was never entered,
        # so the gap pass must not resurrect it.
        controller, _ = _controller(_gap_plan())
        controller.update_answers(
            {"sections.a.in_network": "Yes", "sections.cov.deductible": "Met"}
        )
        controller.note_task_entered(0)  # intro (has a gap: rep_name)
        controller.note_task_entered(2)  # coverage (gated_task skipped by a flow rule)
        successor = await controller.advance_from(2)
        assert successor is controller.gap_agents[0]  # land on intro, never gated_task
        assert controller._next_gap_task(1) is None  # gated_task (1) is not swept

    @pytest.mark.asyncio
    async def test_skip_to_a_completed_task_during_the_gap_pass_is_a_no_op(self) -> None:
        # The pass re-enters an EARLY task, so active_task_index moves BACKWARDS. Measuring
        # "forward" against progress (not that cursor) keeps a skip from redirecting into a
        # task the call already completed and re-speaking its intro to the rep.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        for i in (0, 1, 2):
            controller.note_task_entered(i)
        gap_agent = await controller.advance_from(2)
        assert gap_agent is controller.gap_agents[0]
        controller.note_task_entered(0)  # what GapTaskAgent.on_enter does: index goes back
        session, order = _attach_ordered_session(controller)
        # coverage_task (2) is ahead of the gap cursor (0) but already finished
        await controller.apply_directive_now(SkipToTask(rule_key="r", task_key="coverage_task"))
        session.update_agent.assert_not_called()
        session.interrupt.assert_not_awaited()
        assert order == []

    @pytest.mark.asyncio
    async def test_terminate_still_ends_the_call_during_the_gap_pass(self) -> None:
        # A terminate is a legitimate redirect mid-sweep: the call really is over.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        for i in (0, 1, 2):
            controller.note_task_entered(i)
        await controller.advance_from(2)  # into the gap pass
        controller.note_task_entered(0)
        session, _order = _attach_ordered_session(controller)
        await controller.apply_directive_now(Terminate(rule_key="not_covered"))
        session.update_agent.assert_called_once_with(controller.wrap_up_agent)
        assert controller._terminated is True

    @pytest.mark.asyncio
    async def test_terminated_call_skips_the_gap_pass(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.note_task_entered(0)  # intro has a gap
        _session, _order = _attach_ordered_session(controller)
        await controller.apply_directive_now(Terminate(rule_key="not_covered"))
        assert controller._terminated is True
        controller.note_task_entered(2)
        successor = await controller.advance_from(2)  # advancing into the closer
        assert successor is controller.agents[_CLOSER]  # no gap pass — call was ended


class TestGapAgent:
    @pytest.mark.asyncio
    async def test_on_enter_rearms_cursor_and_reasks_missing_fields(self) -> None:
        controller, state = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        agent = controller.gap_agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        # cursor points at the OWNING task index so the Observer captures the answer
        assert controller.active_task_index == 0
        assert state.cursor_writes == [(ROOM, "intro_task")]
        instructions = mock_session.generate_reply.call_args.kwargs["instructions"]
        assert "Representative name" in instructions
        # neutral follow-up framing — must not imply the call is ending
        assert "follow-up" in instructions
        assert "wrapping up" not in instructions

    @pytest.mark.asyncio
    async def test_gap_complete_advances_forward_then_to_the_closer(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        for i in (0, 1, 2):  # intro, gated, coverage all visited with gaps
            controller.note_task_entered(i)
        first = controller.gap_agents[0]
        with _session_patch(first, MagicMock()):
            second = await _tool(first, "gap_complete")()
        assert second is controller.gap_agents[1]
        with _session_patch(second, MagicMock()):
            third = await _tool(second, "gap_complete")()
        assert third is controller.gap_agents[2]
        with _session_patch(third, MagicMock()):
            after = await _tool(third, "gap_complete")()
        assert after is controller.agents[_CLOSER]  # closer, not wrap-up

    @pytest.mark.asyncio
    async def test_answering_during_the_pass_drops_the_next_gap(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.note_task_entered(0)
        controller.note_task_entered(2)
        first = controller.gap_agents[0]
        # rep answers the coverage gap while the intro gap is being handled
        controller.update_answers({"sections.cov.deductible": "Met"})
        with _session_patch(first, MagicMock()):
            successor = await _tool(first, "gap_complete")()
        assert successor is controller.agents[_CLOSER]

    @pytest.mark.asyncio
    async def test_on_enter_with_no_gaps_moves_on_without_speaking(self) -> None:
        # Race: the Observer filled the gap between routing and entry.
        controller, _ = _controller(_gap_plan())
        controller.note_task_entered(2)
        controller.update_answers(
            {"sections.intro.rep_name": "Pat", "sections.cov.deductible": "Met"}
        )
        agent = controller.gap_agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
        mock_session.generate_reply.assert_not_called()
        mock_session.update_agent.assert_called_once_with(controller.agents[_CLOSER])

    @pytest.mark.asyncio
    async def test_gap_agent_is_dialogue_only(self) -> None:
        controller, _ = _controller(_gap_plan())
        names = [t.info.name for t in controller.gap_agents[0].tools if isinstance(t, FunctionTool)]
        assert names == ["gap_complete"]

    @pytest.mark.asyncio
    async def test_on_enter_no_op_under_takeover(self) -> None:
        controller, _ = _controller(_gap_plan())
        agent = controller.gap_agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            mock_session.userdata.engaged = True
            await agent.on_enter()
        mock_session.generate_reply.assert_not_called()
        mock_session.update_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_gap_complete_no_op_under_takeover(self) -> None:
        controller, _ = _controller(_gap_plan())
        agent = controller.gap_agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            mock_session.userdata.engaged = True
            result = await _tool(agent, "gap_complete")()
        assert isinstance(result, str)
        assert "supervisor" in result.lower()


class TestGapAgentConstruction:
    def test_one_gap_agent_per_sweepable_task_built_up_front(self) -> None:
        # Four tasks, but the closer is never swept — so no gap agent is built for it.
        plan = _gap_plan()
        controller, _ = _controller(plan)
        assert len(controller.gap_agents) == len(plan.tasks) - 1
        assert all(isinstance(a, GapTaskAgent) for a in controller.gap_agents)

    def test_gap_agent_instructions_fuse_session_and_title(self) -> None:
        controller, _ = _controller(_gap_plan())
        instructions = controller.gap_agents[0].instructions
        assert "P." in instructions and "Introduction" in instructions
        assert "gap_complete" in instructions

    def test_gap_agent_instructions_forbid_finality_language(self) -> None:
        # A gap agent is a mid-call follow-up, not the finale — it must not be told to
        # wrap up (regression: it announced "I have all the information I need" mid-pass).
        controller, _ = _controller(_gap_plan())
        instructions = controller.gap_agents[0].instructions
        assert "Final gap check" not in instructions
        assert "do NOT say goodbye" in instructions
        assert "more questions may still follow" in instructions


def _linear_plan(count: int) -> CallPlan:
    """`count` ungated tasks, so a walk visits every one in order."""
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(task_key=f"task{i}", title=f"Task {i}", prompt=f"Prompt {i}")
            for i in range(count)
        ],
    )


async def _walk(controller: PlanRunController, upto: int) -> Agent:
    """Drive `task_complete` from task 0 through task `upto - 1`, giving each agent one
    distinctive turn of its own first. Returns the agent the chain lands on."""
    current: Agent = controller.agents[0]
    for index in range(upto):
        agent = controller.agents[index]
        controller.note_task_entered(index)  # on_enter's job; the walk drives tools directly
        agent._chat_ctx.add_message(role="user", content=f"turn-from-task{index}")
        with _session_patch(agent, MagicMock()):
            current = await _insist_complete(agent)
    return current


async def _insist_complete(agent: Agent) -> Agent:
    """`task_complete`, retried through one refusal.

    These walks leave required questions unanswered on purpose — that is what gives the gap
    pass something to sweep — so the premature-completion guard refuses the first call. It
    refuses only once, and the walk is asserting the handoff chain, not the guard."""
    result = await _tool(agent, "task_complete")()
    if isinstance(result, str):
        result = await _tool(agent, "task_complete")()
    assert isinstance(result, Agent), result
    return result


async def _turn_ctx_after_user_turn(agent: Agent, *, pending_note: str | None = None) -> Any:
    """Run `agent`'s user-turn hook with `pending_note` queued, returning the turn context."""
    mock_session = MagicMock()
    with _session_patch(agent, mock_session):
        if pending_note is not None:
            push_coaching_note(mock_session, pending_note)
        turn_ctx = agent.chat_ctx.copy()
        await agent.on_user_turn_completed(
            turn_ctx, new_message=ChatMessage(role="user", content=["hi"])
        )
    return turn_ctx


class TestCoachingNotes:
    """One test per agent class, because each carries its own `on_user_turn_completed`
    override; the apply logic itself is tested in test_coaching.py."""

    @pytest.mark.asyncio
    async def test_plan_task_agent_applies_pending_notes(self) -> None:
        controller, _ = _controller()
        turn_ctx = await _turn_ctx_after_user_turn(
            controller.agents[0], pending_note="coached note"
        )
        assert "coached note" in ctx_texts(turn_ctx)

    @pytest.mark.asyncio
    async def test_gap_task_agent_applies_pending_notes(self) -> None:
        controller, _ = _controller()
        turn_ctx = await _turn_ctx_after_user_turn(
            GapTaskAgent(controller, 0), pending_note="coached note"
        )
        assert "coached note" in ctx_texts(turn_ctx)

    @pytest.mark.asyncio
    async def test_wrap_up_agent_applies_pending_notes(self) -> None:
        controller, _ = _controller()
        turn_ctx = await _turn_ctx_after_user_turn(
            controller.wrap_up_agent, pending_note="coached note"
        )
        assert "coached note" in ctx_texts(turn_ctx)

    @pytest.mark.asyncio
    async def test_no_pending_notes_is_a_noop(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[0]
        turn_ctx = await _turn_ctx_after_user_turn(agent)
        assert len(turn_ctx.items) == len(agent.chat_ctx.items)


class TestPreviousTaskWindow:
    """The handoff carries ONLY the previous task's own turns — the window is one task deep,
    instead of the whole call so far (which grows linearly to wrap-up)."""

    async def test_carries_only_the_previous_task(self) -> None:
        controller, _ = _controller(_linear_plan(5), previous_task_only=True)
        landed = await _walk(controller, 4)
        assert landed is controller.agents[4]
        # Task 3 is the immediate predecessor; everything before it has aged out.
        assert chat_ctx_texts(landed) == ["turn-from-task3"]

    async def test_windowed_by_default(self) -> None:
        # The window is the DEFAULT, so a controller built without the kwarg must be bounded —
        # the previous assertion only proved the flag works when passed explicitly.
        controller = PlanRunController(
            _linear_plan(5), room_name=ROOM, run_state=cast(Any, FakeRunState())
        )
        landed = await _walk(controller, 4)
        assert chat_ctx_texts(landed) == ["turn-from-task3"]

    async def test_cumulative_when_disabled(self) -> None:
        # The opt-out must still restore the pre-window behavior byte for byte.
        controller, _ = _controller(_linear_plan(5), previous_task_only=False)
        landed = await _walk(controller, 4)
        assert chat_ctx_texts(landed) == [f"turn-from-task{i}" for i in range(4)]

    async def test_wrap_up_gets_only_the_closing_task(self) -> None:
        controller, _ = _controller(_linear_plan(4), previous_task_only=True)
        landed = await _walk(controller, 4)
        assert landed is controller.wrap_up_agent
        assert chat_ctx_texts(landed) == ["turn-from-task3"]

    async def test_directive_swap_seeds_the_inheritance_boundary(self) -> None:
        # `update_agent` used to swap with no carry at all, so its target got no boundary
        # either — and the NEXT handoff would treat the whole inherited context as that
        # agent's own turns, silently reverting to cumulative. Regression guard.
        controller, _ = _controller(_linear_plan(5), previous_task_only=True)
        await _walk(controller, 2)
        live = controller.agents[2]
        live._chat_ctx.add_message(role="user", content="turn-from-task2")
        session = MagicMock()
        session.userdata = TakeoverState()
        session.interrupt = AsyncMock()
        session.current_agent = live
        controller.attach_session(session)
        await controller.apply_directive_now(SkipToTask(rule_key="r", task_key="task4"))
        session.update_agent.assert_called_once_with(controller.agents[4])

        target = controller.agents[4]
        assert chat_ctx_texts(target) == ["turn-from-task2"]
        # The boundary is what matters: task 4's own handoff must not re-carry the above.
        target._chat_ctx.add_message(role="user", content="turn-from-task4")
        with _session_patch(target, MagicMock()):
            successor = cast(Agent, await _tool(target, "task_complete")())
        assert chat_ctx_texts(successor) == ["turn-from-task4"]

    async def test_the_closer_sees_the_substantive_task_through_a_gap_agent(self) -> None:
        # The gap pass inserts a hop between the last substantive task and the closer. A gap
        # agent is not a task, so it must be TRANSPARENT: otherwise "one task deep" would leave
        # the closer looking at a re-ask exchange and nothing else.
        controller, _ = _controller(_gap_plan(), previous_task_only=True)
        controller.update_answers({"sections.a.in_network": "Yes"})  # rep_name missing -> gap
        gap = await _walk(controller, 3)
        assert isinstance(gap, GapTaskAgent)
        gap._chat_ctx.add_message(role="assistant", content="re-ask from the gap pass")
        with _session_patch(gap, MagicMock()):
            successor = cast(Agent, await _tool(gap, "gap_complete")())
        texts = chat_ctx_texts(successor)
        assert "turn-from-task2" in texts  # the substantive task it came from
        assert "re-ask from the gap pass" in texts  # plus what the gap agent actually said

    async def test_a_silent_gap_agent_does_not_wipe_its_successor(self) -> None:
        # `GapTaskAgent.on_enter` swaps on WITHOUT SPEAKING when the Observer answered its
        # fields between selection and entry. Such an agent has no turns of its own, so a
        # naive "carry the source's own turns" hands the successor an EMPTY context — and the
        # successor here is the closer, which collects the reference number and says goodbye.
        controller, _ = _controller(_gap_plan(), previous_task_only=True)
        controller.update_answers({"sections.a.in_network": "Yes"})  # rep_name missing -> gap
        gap = await _walk(controller, 3)
        assert isinstance(gap, GapTaskAgent)
        # The Observer now answers everything, so this gap agent has nothing left to re-ask.
        controller.update_answers(
            {
                "sections.a.in_network": "Yes",
                "sections.intro.rep_name": "Martha",
                "sections.gated.copay": "$20",
                "sections.cov.deductible": "Met",
            }
        )
        mock_session = MagicMock()
        with _session_patch(gap, mock_session):
            await gap.on_enter()
        successor = mock_session.update_agent.call_args.args[0]
        assert chat_ctx_texts(successor), "a silent gap agent handed over an empty context"

    async def test_gap_agent_gets_its_own_task_turns(self) -> None:
        # The gap pass walks BACKWARDS, so the chronological predecessor is a late task.
        # A gap agent re-asks its OWN task's fields, so it needs that task's turns.
        controller, _ = _controller(_gap_plan(), previous_task_only=True)
        controller.update_answers({"sections.a.in_network": "Yes"})  # rep_name still missing
        landed = await _walk(controller, 3)  # completing task 2 diverts into the gap pass
        assert isinstance(landed, GapTaskAgent)
        assert landed.task_index == 0
        texts = chat_ctx_texts(landed)
        assert "turn-from-task0" in texts  # the swept task's own turns
        assert "turn-from-task2" in texts  # the source it arrived from
        assert "turn-from-task1" not in texts  # not the whole call


_OPENING = "Hello, I'm VERA. This call is recorded for quality and training purposes."


def _pin_opening(controller: PlanRunController, agent: Agent) -> ChatMessage:
    """Pin an opening the way `on_enter` does: the item the `say` added, by identity."""
    message = agent._chat_ctx.add_message(role="assistant", content=_OPENING)
    controller.note_opening_spoken([message])
    return message


async def _inherit_opening(controller: PlanRunController, *replies: str) -> None:
    """Hand the first task agent an opening it did NOT speak, the way
    `ivr_agent.transfer_to_verification` does — nothing pinned it, so only `_ensure_anchor`'s
    positional fallback can find it."""
    navigator = Agent(instructions="navigate the menu")
    navigator._chat_ctx.add_message(role="assistant", content=_OPENING)
    for reply in replies:
        navigator._chat_ctx.add_message(role="user", content=reply)
    await carry_chat_ctx(navigator, controller.first_agent())


class TestOpeningAnchor:
    """The call's opening — greeting plus the recording/identity disclosure — is PINNED: it leads
    every carry set however deep the walk goes, while the rest of the window stays one task deep.
    Without it a seven-task walk hands the closer a context in which VERA never introduced
    herself, and she re-introduces herself to a rep she already greeted."""

    async def test_on_enter_pins_the_spoken_opening(self) -> None:
        controller, _ = _controller()
        opener = controller.agents[0]
        message = opener._chat_ctx.add_message(role="assistant", content=_OPENING)
        session = MagicMock()
        with _session_patch(opener, session):
            session.say.return_value.chat_items = [message]
            await opener.on_enter()
        assert [item.id for item in controller._anchor_items] == [message.id]

    async def test_a_later_task_intro_does_not_overwrite_the_opening(self) -> None:
        # Only the CALL's opening is an introduction; task 2's intro is just a section header.
        controller, _ = _controller()
        message = _pin_opening(controller, controller.agents[0])
        later = controller.agents[2]
        session = MagicMock()
        with _session_patch(later, session):
            session.say.return_value.chat_items = [
                later._chat_ctx.add_message(role="assistant", content="Next up.")
            ]
            await later.on_enter()
        assert [item.id for item in controller._anchor_items] == [message.id]

    async def test_opening_survives_a_seven_task_walk(self) -> None:
        # The eval failure (`test_opening_survives_to_the_end_of_the_call`) at unit level: the
        # opening leads, and everything but the immediate predecessor has still aged out.
        controller, _ = _controller(_linear_plan(8), previous_task_only=True)
        _pin_opening(controller, controller.agents[0])
        landed = await _walk(controller, 7)
        assert chat_ctx_texts(landed) == [_OPENING, "turn-from-task6"]

    async def test_wrap_up_gets_the_opening_and_the_closing_task(self) -> None:
        controller, _ = _controller(_linear_plan(4), previous_task_only=True)
        _pin_opening(controller, controller.agents[0])
        landed = await _walk(controller, 4)
        assert landed is controller.wrap_up_agent
        assert chat_ctx_texts(landed) == [_OPENING, "turn-from-task3"]

    async def test_anchor_is_not_duplicated_on_the_first_hop(self) -> None:
        # On the first hop the opener IS the source, so the opening is in the carry set twice.
        # `carry_items` dedupes on id keeping the first, so it stays single and stays in front.
        controller, _ = _controller(_linear_plan(3), previous_task_only=True)
        _pin_opening(controller, controller.agents[0])
        landed = await _walk(controller, 1)
        texts = chat_ctx_texts(landed)
        assert texts.count(_OPENING) == 1
        assert texts == [_OPENING, "turn-from-task0"]

    async def test_anchor_falls_back_to_an_inherited_opening(self) -> None:
        controller, _ = _controller(_linear_plan(5), previous_task_only=True)
        await _inherit_opening(controller)
        landed = await _walk(controller, 4)
        texts = chat_ctx_texts(landed)
        assert texts[0] == _OPENING
        assert "turn-from-task3" in texts  # the predecessor, still there
        assert "turn-from-task1" not in texts  # and the window is still one task deep

    async def test_anchor_closes_the_exchange_with_the_reps_reply(self) -> None:
        # A lone opening question invites the closer to re-ask it; the rep's reply comes along so
        # the pinned pair reads as an exchange already had.
        controller, _ = _controller(_linear_plan(5), previous_task_only=True)
        await _inherit_opening(controller, "Yes, that's the member.")
        landed = await _walk(controller, 4)
        assert chat_ctx_texts(landed) == [_OPENING, "Yes, that's the member.", "turn-from-task3"]

    async def test_a_later_assistant_turn_is_never_mistaken_for_the_opening(self) -> None:
        # The fallback is positional, so it is gated to the FIRST handoff — otherwise a gap
        # agent's re-ask would be pinned as the call's introduction.
        controller, _ = _controller(_gap_plan(), previous_task_only=True)
        controller.update_answers({"sections.a.in_network": "Yes"})  # rep_name missing -> gap
        gap = await _walk(controller, 3)
        assert isinstance(gap, GapTaskAgent)
        gap._chat_ctx.add_message(role="assistant", content="re-ask from the gap pass")
        with _session_patch(gap, MagicMock()):
            successor = cast(Agent, await _tool(gap, "gap_complete")())
        assert controller._anchor_items == []
        assert _OPENING not in chat_ctx_texts(successor)

    async def test_the_opening_leads_a_gap_agents_context(self) -> None:
        # A re-ask agent that cannot see the opening is the same re-introduction hazard.
        controller, _ = _controller(_gap_plan(), previous_task_only=True)
        _pin_opening(controller, controller.agents[0])
        controller.update_answers({"sections.a.in_network": "Yes"})  # rep_name still missing
        landed = await _walk(controller, 3)
        assert isinstance(landed, GapTaskAgent)
        texts = chat_ctx_texts(landed)
        assert texts[0] == _OPENING
        assert texts.index(_OPENING) < texts.index("turn-from-task0")

    async def test_a_silent_gap_agent_still_passes_the_opening_through(self) -> None:
        # `_own_turns`' whole-context escape hatch for a silent gap agent must compose with the
        # anchor: the closer still gets the opening, and still exactly once.
        controller, _ = _controller(_gap_plan(), previous_task_only=True)
        _pin_opening(controller, controller.agents[0])
        controller.update_answers({"sections.a.in_network": "Yes"})  # rep_name missing -> gap
        gap = await _walk(controller, 3)
        assert isinstance(gap, GapTaskAgent)
        controller.update_answers(
            {
                "sections.a.in_network": "Yes",
                "sections.intro.rep_name": "Martha",
                "sections.gated.copay": "$20",
                "sections.cov.deductible": "Met",
            }
        )
        mock_session = MagicMock()
        with _session_patch(gap, mock_session):
            await gap.on_enter()
        successor = mock_session.update_agent.call_args.args[0]
        texts = chat_ctx_texts(successor)
        assert texts.count(_OPENING) == 1
        assert texts[0] == _OPENING

    async def test_cumulative_mode_is_unaffected_by_the_anchor(self) -> None:
        # The opt-out already carries the whole call, so it must not gain a prepended copy.
        controller, _ = _controller(_linear_plan(5), previous_task_only=False)
        _pin_opening(controller, controller.agents[0])
        landed = await _walk(controller, 4)
        assert chat_ctx_texts(landed) == [_OPENING] + [f"turn-from-task{i}" for i in range(4)]

    async def test_directive_swap_carries_the_opening(self) -> None:
        # `apply_directive_now` is the other path into wrap-up, and it goes through the same seam.
        controller, _ = _controller(_linear_plan(5), previous_task_only=True)
        _pin_opening(controller, controller.agents[0])
        await _walk(controller, 2)
        live = controller.agents[2]
        live._chat_ctx.add_message(role="user", content="turn-from-task2")
        session = MagicMock()
        session.userdata = TakeoverState()
        session.interrupt = AsyncMock()
        session.current_agent = live
        controller.attach_session(session)
        await controller.apply_directive_now(Terminate(rule_key="r"))
        assert chat_ctx_texts(controller.wrap_up_agent) == [_OPENING, "turn-from-task2"]


_MALE_GATE = Comparison(field="sections.patient.spouse_gender", op="eq", value="Male")


def _gated_task_plan() -> CallPlan:
    """Three tasks; the MIDDLE one's every question is gated on a value that never holds — the
    shape of the schema's `male_partner` task on a call with no male spouse."""
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(
                task_key="basics",
                title="Basics",
                intro="Hello rep.",
                outro="Noted.",
                prompt="Basics.",
                fields=[_field("sections.a.plan_type", "Plan type")],
            ),
            PlanTask(
                task_key="male_partner",
                title="Male Partner Coverage",
                intro="Now I'd like to ask about male partner fertility coverage.",
                outro="Thanks, that covers the male partner benefits.",
                prompt="Male partner.",
                fields=[
                    _field("sections.male.covered", "Male partner covered", gates=(_MALE_GATE,)),
                    _field("sections.male.cpt_89320", "CPT 89320", gates=(_MALE_GATE,)),
                ],
            ),
            PlanTask(
                task_key="closing_task",
                title="Wrap Up",
                prompt="Close.",
                fields=[_field("sections.rep.name", "Representative name")],
            ),
        ],
    )


class TestGatedOutTask:
    """A task whose every question is excluded by its gates must be handed straight on. The
    compiled prompt is rendered per schema, before any answer exists, so it lists every question
    and expresses each gate only as prose the model must evaluate against a value it may never
    have been given — which is how the eval judge caught VERA asking all of them."""

    def test_applicable_fields_drops_the_gated_ones(self) -> None:
        controller, _ = _controller(_gated_task_plan())
        assert controller.applicable_fields(1) == []
        assert [f.title for f in controller.inapplicable_fields(1)] == [
            "Male partner covered",
            "CPT 89320",
        ]

    def test_the_gate_holding_makes_them_applicable_again(self) -> None:
        controller, _ = _controller(_gated_task_plan())
        controller.update_answers({"sections.patient.spouse_gender": "Male"})
        assert len(controller.applicable_fields(1)) == 2
        assert controller.inapplicable_fields(1) == []

    @pytest.mark.asyncio
    async def test_a_fully_gated_task_is_skipped_without_speaking(self) -> None:
        controller, _ = _controller(_gated_task_plan())
        controller.opening_line("Hello rep.")  # the call has already opened
        gated = controller.agents[1]
        mock_session = MagicMock()
        with _session_patch(gated, mock_session):
            await gated.on_enter()
        mock_session.say.assert_not_called()  # no "Now I'd like to ask about male partner…"
        mock_session.generate_reply.assert_not_called()
        assert mock_session.update_agent.call_args.args[0] is controller.agents[2]

    @pytest.mark.asyncio
    async def test_a_skipped_task_leaves_the_closer_to_sign_off(self) -> None:
        # It speaks no outro, so the flag `note_task_outro` sets must not be left True by the
        # task before it — otherwise wrap-up closes silently and nobody says goodbye.
        controller, _ = _controller(_gated_task_plan())
        controller.opening_line("Hello rep.")
        basics = controller.agents[0]
        with _session_patch(basics, MagicMock()):
            await _insist_complete(basics)
        assert controller.signed_off, "the first task's outro was not recorded"
        with _session_patch(controller.agents[1], MagicMock()):
            await controller.agents[1].on_enter()
        assert not controller.signed_off

    @pytest.mark.asyncio
    async def test_a_skipped_task_still_counts_as_visited(self) -> None:
        # The cursor and the visited set drive the gap pass and the compiled-order assertion; a
        # skipped task is entered, it just says nothing.
        controller, _ = _controller(_gated_task_plan())
        controller.opening_line("Hello rep.")
        with _session_patch(controller.agents[1], MagicMock()):
            await controller.agents[1].on_enter()
        assert 1 in controller._visited_tasks

    @pytest.mark.asyncio
    async def test_the_opening_task_is_never_skipped_silently(self) -> None:
        # The greeting and the recording/identity disclosure ride on the opening task's intro.
        plan = _gated_task_plan()
        plan.tasks[0] = plan.tasks[0].model_copy(
            update={"fields": [_field("sections.a.x", "Gated", gates=(_MALE_GATE,))]}
        )
        controller, _ = _controller(plan)
        opener = controller.agents[0]
        mock_session = MagicMock()
        with _session_patch(opener, mock_session):
            await opener.on_enter()
        mock_session.say.assert_called_once_with("Hello rep.")
        mock_session.update_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_task_with_no_fields_at_all_still_runs(self) -> None:
        # `_plan`'s tasks carry only speech; "no applicable fields" must not mean "no fields".
        controller, _ = _controller()
        controller.opening_line("Hello rep.")
        agent = controller.agents[2]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
        mock_session.say.assert_called_once_with("Next up.")
        mock_session.update_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_partly_gated_task_excludes_only_the_gated_question(self) -> None:
        # The other half of the same defect: the task runs, but the model must be told which of
        # its listed questions are out of scope this call — and only those. See TestGating for
        # why this lives in the instructions rather than the per-reply lead.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})  # gates out `oon_note`
        controller.opening_line("Hello rep.")
        coverage = controller.agents[2]
        with _session_patch(coverage, MagicMock()):
            await coverage.on_enter()
        excluded_section = coverage.instructions.split("do NOT ask these")[1]
        assert "OON note" in excluded_section
        assert "Deductible" not in excluded_section  # the applicable one is not excluded

    @pytest.mark.asyncio
    async def test_an_ungated_task_keeps_the_plain_lead(self) -> None:
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "No"})  # every coverage field applies
        controller.opening_line("Hello rep.")
        coverage = controller.agents[2]
        mock_session = MagicMock()
        with _session_patch(coverage, mock_session):
            await coverage.on_enter()
        assert mock_session.generate_reply.call_args.kwargs["instructions"] == _OPENING_DIRECTIVE
