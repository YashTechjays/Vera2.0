"""Plan runtime — one PlanTaskAgent per CallPlan task, sequential tool handoff,
skip-scan on applicable_when, wrap-up, and the agent-owned cursor write."""

import asyncio
import uuid
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from conftest import chat_ctx_texts
from livekit.agents import Agent
from livekit.agents.llm import FunctionTool

from agent_worker.plan_runtime import (
    WRAP_UP_TASK_KEY,
    PlanRunController,
    PlanTaskAgent,
    WrapUpAgent,
)
from vera_core.forms.call_plan import CallPlan, PlanSession, PlanTask
from vera_core.forms.dsl import Comparison

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
) -> tuple[PlanRunController, FakeRunState]:
    state = run_state or FakeRunState()
    controller = PlanRunController(
        plan or _plan(),
        room_name=ROOM,
        run_state=cast(Any, state),
        greeting=greeting,
        extra_instructions=extra_instructions,
    )
    return controller, state


def _tool(agent: Agent, name: str) -> FunctionTool:
    return next(t for t in agent.tools if isinstance(t, FunctionTool) and t.info.name == name)


def _session_patch(agent: Agent, mock_session: MagicMock) -> Any:
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


class TestDirectiveSeam:
    @pytest.mark.asyncio
    async def test_apply_directive_is_a_phase_2_seam(self) -> None:
        controller, _ = _controller()
        with pytest.raises(NotImplementedError):
            await controller.apply_directive(object())

    @pytest.mark.asyncio
    async def test_handoffs_serialize_on_the_controller_lock(self) -> None:
        controller, _ = _controller()
        async with controller.lock:
            # a concurrent task_complete must wait for the lock, not double-swap
            task = asyncio.create_task(controller.advance_from(0))
            await asyncio.sleep(0)
            assert not task.done()
        assert await task is controller.agents[2]


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
