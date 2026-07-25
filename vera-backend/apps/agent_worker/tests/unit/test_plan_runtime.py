"""Plan runtime — one PlanTaskAgent per CallPlan task, sequential tool handoff,
skip-scan on applicable_when, wrap-up, and the agent-owned cursor write."""

import asyncio
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import chat_ctx_texts
from livekit.agents import Agent
from livekit.agents.llm import FunctionTool

from agent_worker.directives import ReAsk, SkipToTask, Terminate
from agent_worker.intervention import TakeoverState
from agent_worker.plan_runtime import (
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
            PlanTask(task_key="last_task", title="Last", prompt="Finish up."),
        ],
    )


def _controller(
    plan: CallPlan | None = None,
    run_state: FakeRunState | None = None,
    *,
    greeting: str | None = None,
    extra_instructions: str | None = None,
    gap_pass_enabled: bool = True,
) -> tuple[PlanRunController, FakeRunState]:
    state = run_state or FakeRunState()
    controller = PlanRunController(
        plan or _plan(),
        room_name=ROOM,
        run_state=cast(Any, state),
        greeting=greeting,
        extra_instructions=extra_instructions,
        gap_pass_enabled=gap_pass_enabled,
    )
    return controller, state


def _tool(agent: Agent, name: str) -> FunctionTool:
    return next(t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == name)


def _session_patch(agent: Agent, mock_session: MagicMock) -> Any:
    # A real latch: a bare MagicMock attribute reads truthy and trips the takeover guards.
    mock_session.userdata = TakeoverState()
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


class TestHandoff:
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
    async def test_inapplicable_task_is_skipped(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[0]
        # gated_task requires in_network == "Yes"; unanswered ("") → skip to last_task
        with _session_patch(agent, MagicMock()):
            handoff = await _tool(agent, "task_complete")()
        assert handoff is controller.agents[2]

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
    async def test_says_intro_and_writes_cursor(self) -> None:
        controller, state = _controller()
        agent = controller.agents[0]
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        mock_session.say.assert_called_once_with("Hello rep.")
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

    @pytest.mark.asyncio
    async def test_no_intro_no_speech(self) -> None:
        controller, _ = _controller()
        agent = controller.agents[1]  # gated_task has no intro
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        mock_session.say.assert_not_called()

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

    @pytest.mark.asyncio
    async def test_wrap_up_end_call_shuts_down(self) -> None:
        controller, _ = _controller()
        agent = controller.wrap_up_agent
        mock_session = MagicMock()
        with _session_patch(agent, mock_session):
            result = await _tool(agent, "end_call")()
        assert result == "Call ended."
        mock_session.shutdown.assert_called_once_with(drain=True)


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
    def test_one_gap_agent_per_task_built_up_front(self) -> None:
        controller, _ = _controller(_gap_plan())
        assert len(controller.gap_agents) == 4
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
