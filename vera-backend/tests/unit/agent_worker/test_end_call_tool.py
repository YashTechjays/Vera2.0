"""Verify the end_call tool is registered and triggers session shutdown."""

from unittest.mock import MagicMock, patch

import pytest
from livekit.agents.llm import FunctionTool

from agent_worker.agent import VeraAgent
from agent_worker.intervention import TakeoverState


def _make_agent() -> VeraAgent:
    return VeraAgent(instructions="test")


def _function_tools(agent: VeraAgent) -> list[FunctionTool]:  # type: ignore[type-arg]
    return [t for t in agent.tools if isinstance(t, FunctionTool)]


def test_end_call_tool_is_registered() -> None:
    agent = _make_agent()
    tool_names = [t.info.name for t in _function_tools(agent)]
    assert "end_call" in tool_names


@pytest.mark.asyncio
async def test_end_call_triggers_shutdown() -> None:
    agent = _make_agent()
    mock_session = MagicMock()
    # Explicit: a bare MagicMock attribute is truthy, which would trip the takeover guard.
    mock_session.userdata = TakeoverState(engaged=False)

    end_call_tool = next(t for t in _function_tools(agent) if t.info.name == "end_call")

    # Patch the session property so _end_call can access it without a live activity
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        await end_call_tool()

    mock_session.shutdown.assert_called_once_with(drain=True)


@pytest.mark.asyncio
async def test_end_call_refuses_once_a_supervisor_has_taken_over() -> None:
    """The call must never be hung up under a human takeover — reachable when the LLM's
    end_call is already in flight as engage() interrupts it."""
    agent = _make_agent()
    mock_session = MagicMock()
    mock_session.userdata = TakeoverState(engaged=True)

    end_call_tool = next(t for t in _function_tools(agent) if t.info.name == "end_call")

    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        result = await end_call_tool()

    mock_session.shutdown.assert_not_called()
    assert result != "Call ended."
