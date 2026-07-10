"""FanOutTurnPublisher — one TurnPublisher that fans a finalized turn out to many sinks,
so the barge-in ReorderingEmitter attaches once per job instead of once per stream."""

import logging

import pytest

from agent_worker.transcript_publisher import FanOutTurnPublisher


class _RecordingSink:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str, str, int]] = []

    async def publish_turn(self, room_name: str, role: str, text: str, *, ts: int) -> None:
        self.calls.append((room_name, role, text, ts))


class _BoomSink:
    async def publish_turn(self, room_name: str, role: str, text: str, *, ts: int) -> None:
        raise RuntimeError("sink down")


@pytest.mark.asyncio
async def test_publishes_to_all_sinks_in_registration_order() -> None:
    order: list[str] = []

    class _OrderSink:
        def __init__(self, name: str) -> None:
            self._name = name

        async def publish_turn(self, room_name: str, role: str, text: str, *, ts: int) -> None:
            order.append(self._name)

    fan = FanOutTurnPublisher([_OrderSink("a"), _OrderSink("b"), _OrderSink("c")])
    await fan.publish_turn("room", "user", "hello", ts=1)
    assert order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_all_sinks_receive_the_same_turn() -> None:
    a, b = _RecordingSink("a"), _RecordingSink("b")
    fan = FanOutTurnPublisher([a, b])
    await fan.publish_turn("room", "agent", "hi there", ts=42)
    assert a.calls == [("room", "agent", "hi there", 42)]
    assert b.calls == [("room", "agent", "hi there", 42)]


@pytest.mark.asyncio
async def test_a_raising_sink_does_not_block_later_sinks_or_raise() -> None:
    after = _RecordingSink("after")
    fan = FanOutTurnPublisher([_BoomSink(), after])
    await fan.publish_turn("room", "user", "hello", ts=1)  # must not raise
    assert after.calls == [("room", "user", "hello", 1)]


@pytest.mark.asyncio
async def test_raising_sink_is_logged_without_the_turn_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent_worker")
    fan = FanOutTurnPublisher([_BoomSink()])
    await fan.publish_turn("room", "user", "super-secret-phi-text", ts=1)  # must not raise
    assert "super-secret-phi-text" not in caplog.text
    assert "room" in caplog.text
