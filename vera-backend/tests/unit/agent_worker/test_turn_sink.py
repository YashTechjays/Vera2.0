"""_fan_out_sink — collapses the enabled stream sinks behind one TurnPublisher so the worker
never constructs more than one ReorderingEmitter per job."""

from agent_worker.main import _fan_out_sink
from agent_worker.transcript_publisher import FanOutTurnPublisher


class _Sink:
    async def publish_turn(
        self, room_name: str, role: str, text: str, *, ts: int, source: str | None = None
    ) -> None:
        pass


def test_no_sinks_returns_none() -> None:
    assert _fan_out_sink([]) is None


def test_single_sink_is_returned_directly_not_wrapped() -> None:
    sink = _Sink()
    assert _fan_out_sink([sink]) is sink


def test_multiple_sinks_are_wrapped_in_fan_out() -> None:
    result = _fan_out_sink([_Sink(), _Sink()])
    assert isinstance(result, FanOutTurnPublisher)
