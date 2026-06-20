"""Tests for VeraAgent — chat-only persona agent with PHI-wall node overrides."""

from agent_worker.agent import VeraAgent
from vera_core.phi import PassthroughPHIBoundary


def test_vera_agent_is_chat_only_with_persona() -> None:
    agent = VeraAgent(boundary=PassthroughPHIBoundary(), session_id="s1")
    # chat-only: no tools registered
    assert list(agent.tools) == []
    # persona instructions are attached
    assert "infertility" in agent.instructions.lower()
