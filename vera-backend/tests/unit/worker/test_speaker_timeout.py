"""Verify the agent publishes a transcript event when no speaker joins."""

import pytest

from vera_core.transcript import InMemoryTranscriptStore, TranscriptService


@pytest.mark.asyncio
async def test_timeout_publishes_unanswered_message() -> None:
    """When the speaker timeout fires, the entrypoint should publish an agent
    transcript event so the browser user sees feedback, then end the stream."""
    store = InMemoryTranscriptStore()
    service = TranscriptService(store)
    room = "call--test-tenant--test-call"

    # Simulate what entrypoint does on timeout: publish + end
    from agent_worker.main import publish_unanswered_notice

    await publish_unanswered_notice(service, room)

    events = await service.collect(room)
    assert len(events) == 1
    assert events[0].role == "agent"
    assert "not answered" in events[0].text.lower() or "unavailable" in events[0].text.lower()
