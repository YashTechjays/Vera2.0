import pytest

from agent_worker.transcript_publisher import (
    _publish_agent,
    _publish_user,
    attach_transcript_publisher,
)
from vera_core.transcript import ROLE_AGENT, ROLE_USER, InMemoryTranscriptStore, TranscriptService


class _UserEvent:
    def __init__(self, transcript: str, is_final: bool) -> None:
        self.transcript = transcript
        self.is_final = is_final


class _Item:
    def __init__(self, role: str, text: str) -> None:
        self.role = role
        self.text_content = text


class _ItemEvent:
    def __init__(self, item: _Item) -> None:
        self.item = item


def _service() -> TranscriptService:
    return TranscriptService(InMemoryTranscriptStore())


async def _drain(svc: TranscriptService, room: str) -> list[tuple[str, str]]:
    await svc.end(room)
    return [(e.role, e.text) for e in await svc.collect(room)]


@pytest.mark.asyncio
async def test_publishes_final_user_turn() -> None:
    svc = _service()
    await _publish_user(svc, "room", _UserEvent("my id is [[ID_1]]", is_final=True))
    assert await _drain(svc, "room") == [(ROLE_USER, "my id is [[ID_1]]")]


@pytest.mark.asyncio
async def test_skips_interim_and_empty_user_turns() -> None:
    svc = _service()
    await _publish_user(svc, "room", _UserEvent("partial", is_final=False))
    await _publish_user(svc, "room", _UserEvent("   ", is_final=True))
    assert await _drain(svc, "room") == []


@pytest.mark.asyncio
async def test_publishes_assistant_item_only() -> None:
    svc = _service()
    await _publish_agent(svc, "room", _ItemEvent(_Item("assistant", "hello there")))
    await _publish_agent(svc, "room", _ItemEvent(_Item("user", "ignored echo")))
    assert await _drain(svc, "room") == [(ROLE_AGENT, "hello there")]


def test_attach_registers_both_handlers() -> None:
    registered: list[str] = []

    class _FakeSession:
        def on(self, event: str, cb: object) -> None:
            registered.append(event)

    attach_transcript_publisher(_FakeSession(), _service(), "room")
    assert set(registered) == {"user_input_transcribed", "conversation_item_added"}
