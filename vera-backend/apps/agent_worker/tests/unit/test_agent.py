"""Tests for VeraAgent — chat-only persona agent with PHI-wall node overrides."""

from livekit.agents.llm import FunctionTool

from agent_worker.agent import VeraAgent
from agent_worker.prompt import build_instructions
from vera_core.phi import PassthroughPHIBoundary
from vera_core.schemas import PersonaTweak


def test_vera_agent_has_end_call_tool_and_persona() -> None:
    agent = VeraAgent(boundary=PassthroughPHIBoundary(), session_id="s1")
    tool_names = [t.info.name for t in agent.tools if isinstance(t, FunctionTool)]
    assert tool_names == ["end_call"]
    assert "infertility" in agent.instructions.lower()


def test_vera_agent_accepts_overlaid_instructions() -> None:
    instructions = build_instructions(PersonaTweak(extra_instructions="Confirm member ID twice."))
    agent = VeraAgent(
        boundary=PassthroughPHIBoundary(),
        session_id="s1",
        instructions=instructions,
        greeting="Hello there.",
    )
    assert "Confirm member ID twice." in agent.instructions
