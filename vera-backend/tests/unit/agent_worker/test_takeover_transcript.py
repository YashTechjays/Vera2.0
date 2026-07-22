"""Routing and finalized-turn publishing for the takeover transcriber."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from livekit.agents import stt as agents_stt

from agent_worker.takeover_transcript import classify_source, publish_final_turns
from vera_core.transcript import (
    ROLE_USER,
    SOURCE_REP,
    SOURCE_SUPERVISOR,
    TurnRole,
    TurnSource,
)


def test_classify_source() -> None:
    assert classify_source("callee", None, "callee") == SOURCE_REP
    assert classify_source("callee", "listener", "callee") == SOURCE_REP  # callee wins
    assert classify_source("sup-1", "intervener", "callee") == SOURCE_SUPERVISOR
    assert classify_source("sup-1", "listener", "callee") is None  # a watcher is skipped
    assert classify_source("sup-1", None, "callee") is None


class _FakeSink:
    def __init__(self) -> None:
        self.turns: list[tuple[str, TurnRole, str, TurnSource | None]] = []

    async def publish_turn(
        self,
        room_name: str,
        role: TurnRole,
        text: str,
        *,
        ts: int,
        source: TurnSource | None = None,
    ) -> None:
        self.turns.append((room_name, role, text, source))


class _Data:
    def __init__(self, text: str) -> None:
        self.text = text


class _Event:
    def __init__(self, event_type: Any, text: str | None) -> None:
        self.type = event_type
        self.alternatives = [_Data(text)] if text is not None else []


async def _aiter(items: list[_Event]) -> AsyncIterator[_Event]:
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_publish_final_turns_only_finals_stripped_nonempty() -> None:
    sink = _FakeSink()
    events = _aiter(
        [
            _Event(agents_stt.SpeechEventType.INTERIM_TRANSCRIPT, "partial"),  # skipped
            _Event(agents_stt.SpeechEventType.FINAL_TRANSCRIPT, "  hello there  "),
            _Event(agents_stt.SpeechEventType.FINAL_TRANSCRIPT, "   "),  # empty, skipped
            _Event(agents_stt.SpeechEventType.FINAL_TRANSCRIPT, None),  # no alternatives
        ]
    )

    await publish_final_turns(events, sink, "room-1", SOURCE_SUPERVISOR)

    assert sink.turns == [("room-1", ROLE_USER, "hello there", SOURCE_SUPERVISOR)]


@pytest.mark.asyncio
async def test_publish_final_turns_swallows_sink_errors() -> None:
    class _BoomSink(_FakeSink):
        async def publish_turn(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("redis down")

    events = _aiter([_Event(agents_stt.SpeechEventType.FINAL_TRANSCRIPT, "hi")])
    await publish_final_turns(events, _BoomSink(), "room-1", SOURCE_REP)  # must not raise
