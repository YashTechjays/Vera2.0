"""call.health worker event: round-trips through the discriminated adapter."""

import pytest
from pydantic import ValidationError

from vera_core.events import CallHealthEvent, parse_worker_event


def test_call_health_event_roundtrip() -> None:
    ev = CallHealthEvent(
        room_name="call--t--c",
        score=42,
        flag="conversation_loop",
        reason="asked the same question three times",
        turn_count=12,
        ts=1_720_000_000_000,
    )
    parsed = parse_worker_event(ev.model_dump_json())
    assert isinstance(parsed, CallHealthEvent)
    assert parsed == ev


def test_unknown_type_still_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_worker_event('{"type": "call.nonsense", "room_name": "x", "ts": 1}')
