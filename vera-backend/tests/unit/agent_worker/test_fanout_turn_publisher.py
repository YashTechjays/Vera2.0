"""FanOutTurnPublisher — one TurnPublisher that fans a finalized turn out to many sinks,
so the barge-in ReorderingEmitter attaches once per job instead of once per stream."""

import logging

import pytest

from agent_worker.transcript_publisher import FanOutTurnPublisher, ReorderingEmitter

# Sentinel PHI that a failing sink embeds in its exception args, the way redis-py's
# Pipeline.annotate_exception embeds the failed command (incl. the turn text) into
# exception.args. It must NEVER reach a log line.
_PHI = "SECRET_PHI_TOKEN"


class _AnnotatedBoomSink:
    """Raises the way a redis pipeline failure does: exception args contain the failed
    command, including the turn text."""

    async def publish_turn(
        self,
        room_name: str,
        role: str,
        text: str,
        *,
        ts: int,
        source: str | None = None,
        user_id: str | None = None,
    ) -> None:
        raise RuntimeError(f"Command # 1 (XADD stream '*' text {text!r}) of pipeline: OOM")


class _RecordingSink:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str, str, int, str | None]] = []

    async def publish_turn(
        self,
        room_name: str,
        role: str,
        text: str,
        *,
        ts: int,
        source: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.calls.append((room_name, role, text, ts, source))


class _BoomSink:
    async def publish_turn(
        self,
        room_name: str,
        role: str,
        text: str,
        *,
        ts: int,
        source: str | None = None,
        user_id: str | None = None,
    ) -> None:
        raise RuntimeError("sink down")


@pytest.mark.asyncio
async def test_publishes_to_all_sinks_in_registration_order() -> None:
    order: list[str] = []

    class _OrderSink:
        def __init__(self, name: str) -> None:
            self._name = name

        async def publish_turn(
            self,
            room_name: str,
            role: str,
            text: str,
            *,
            ts: int,
            source: str | None = None,
            user_id: str | None = None,
        ) -> None:
            order.append(self._name)

    fan = FanOutTurnPublisher([_OrderSink("a"), _OrderSink("b"), _OrderSink("c")])
    await fan.publish_turn("room", "user", "hello", ts=1)
    assert order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_all_sinks_receive_the_same_turn_including_source() -> None:
    a, b = _RecordingSink("a"), _RecordingSink("b")
    fan = FanOutTurnPublisher([a, b])
    await fan.publish_turn("room", "agent", "hi there", ts=42, source="bot")
    assert a.calls == [("room", "agent", "hi there", 42, "bot")]
    assert b.calls == [("room", "agent", "hi there", 42, "bot")]


@pytest.mark.asyncio
async def test_a_raising_sink_does_not_block_later_sinks_or_raise() -> None:
    after = _RecordingSink("after")
    fan = FanOutTurnPublisher([_BoomSink(), after])
    await fan.publish_turn("room", "user", "hello", ts=1)  # must not raise
    assert after.calls == [("room", "user", "hello", 1, None)]


@pytest.mark.asyncio
async def test_raising_sink_is_logged_without_the_turn_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent_worker")
    fan = FanOutTurnPublisher([_BoomSink()])
    await fan.publish_turn("room", "user", "super-secret-phi-text", ts=1)  # must not raise
    assert "super-secret-phi-text" not in caplog.text
    assert "room" in caplog.text


@pytest.mark.asyncio
async def test_fan_out_never_logs_exception_content_even_when_it_embeds_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Redis pipeline errors annotate exception.args with the failed command — including the
    # turn text. The failure log must carry only the exception TYPE, never its content.
    caplog.set_level(logging.WARNING, logger="agent_worker")
    fan = FanOutTurnPublisher([_AnnotatedBoomSink()])
    await fan.publish_turn("room", "user", _PHI, ts=1)  # must not raise
    rendered = "".join(record.getMessage() for record in caplog.records)
    assert _PHI not in rendered
    assert "RuntimeError" in rendered


@pytest.mark.asyncio
async def test_emitter_drain_never_logs_exception_content_even_when_it_embeds_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same PHI channel one layer up: the ReorderingEmitter's queue drain logs a publish
    # failure — that log must also carry only the exception type, never its content.
    caplog.set_level(logging.WARNING, logger="agent_worker")

    class _UserEvent:
        transcript = _PHI
        is_final = True
        created_at = 1.0

    emitter = ReorderingEmitter(_AnnotatedBoomSink(), "room")
    emitter.on_user(_UserEvent())
    await emitter.aclose()  # drains the queued turn through the raising sink
    rendered = "".join(record.getMessage() for record in caplog.records)
    assert _PHI not in rendered
    assert "RuntimeError" in rendered
