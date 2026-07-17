"""call.ended joins the worker-event union; parse round-trips both types."""

from vera_core.events import CallEndedEvent, CallFailedEvent, parse_worker_event


def test_parse_call_ended_roundtrip() -> None:
    event = CallEndedEvent(room_name="call--t--c", ts=1)
    parsed = parse_worker_event(event.model_dump_json())
    assert isinstance(parsed, CallEndedEvent)
    assert parsed.room_name == "call--t--c"


def test_parse_call_failed_still_works() -> None:
    raw = '{"type":"call.failed","room_name":"r","reason":"no_answer","ts":2}'
    assert isinstance(parse_worker_event(raw), CallFailedEvent)
