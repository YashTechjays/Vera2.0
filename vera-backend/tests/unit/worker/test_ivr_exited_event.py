"""The worker publishes ivr.exited when the navigator reaches a live human."""

from vera_core.events import IvrExitedEvent, parse_worker_event


def test_ivr_exited_round_trips_through_the_wire_format() -> None:
    event = IvrExitedEvent(room_name="call--t--c", ts=1720000000000)
    assert event.type == "ivr.exited"
    assert parse_worker_event(event.model_dump_json()) == event
