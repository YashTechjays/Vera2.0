"""Verify the end_call tool is registered and triggers session shutdown."""

from unittest.mock import MagicMock, patch

import pytest
from livekit.agents.llm import FunctionTool

from agent_worker.agent import VeraAgent
from vera_core.phi import PassthroughPHIBoundary


def _make_agent() -> VeraAgent:
    return VeraAgent(
        boundary=PassthroughPHIBoundary(),
        session_id="test-session",
        instructions="test",
        greeting="hello",
    )


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

    end_call_tool = next(
        t for t in _function_tools(agent) if t.info.name == "end_call"
    )

    # Patch the session property so _end_call can access it without a live activity
    with patch.object(type(agent), "session", new=property(lambda self: mock_session)):
        await end_call_tool()

    mock_session.shutdown.assert_called_once_with(drain=True)
