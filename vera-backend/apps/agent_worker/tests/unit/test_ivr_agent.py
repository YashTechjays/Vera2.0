"""IVR navigator: id sentinel and the transfer-to-verification handoff."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from livekit.agents import Agent
from opentelemetry import trace

from agent_worker.ivr_agent import IVR_NAVIGATOR_ID, IvrNavigatorAgent


def _verifier(agent_id: str) -> Agent:
    """Create a test verifier agent with a specific id."""

    class TestVerifier(Agent):
        def __init__(self, agent_id: str) -> None:
            super().__init__(instructions="verify", id=agent_id)

    return TestVerifier(agent_id)


def _navigator(verifier: Agent) -> IvrNavigatorAgent:
    return IvrNavigatorAgent(verification_agent_factory=lambda: verifier)


def _session_patch(agent: Agent, mock_session: MagicMock) -> Any:
    return patch.object(type(agent), "session", new=property(lambda self: mock_session))


class TestConstruction:
    def test_navigator_id_is_the_sentinel(self) -> None:
        navigator = _navigator(Agent(instructions="verify"))
        assert navigator.id == IVR_NAVIGATOR_ID


class TestHandoff:
    @pytest.mark.asyncio
    async def test_transfer_tags_the_handoff_span(self, otel_spans: Any) -> None:
        verifier = _verifier("intro_task")
        navigator = _navigator(verifier)
        tracer = trace.get_tracer("test")
        mock_session = MagicMock()
        mock_session.interrupt = AsyncMock()
        with _session_patch(navigator, mock_session), tracer.start_as_current_span("probe"):
            handoff = await navigator.transfer_to_verification()
        assert handoff is verifier
        spans = [s for s in otel_spans.get_finished_spans() if s.name == "probe"]
        assert spans, "probe span not found in finished spans"
        span = spans[0]
        assert span.attributes["vera.handoff.from_task"] == IVR_NAVIGATOR_ID
        assert span.attributes["vera.handoff.to_task"] == "intro_task"
        assert span.attributes["vera.handoff.reason"] == "ivr_live_human"
