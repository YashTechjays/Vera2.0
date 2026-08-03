"""The field-answer envelope rides the same per-call stream as transcript/status."""

import pytest

from vera_core.call_stream import TYPE_FIELD_ANSWER, CallStreamEvent, CallStreamService


class _SpyStore:
    def __init__(self) -> None:
        self.published: list[tuple[str, CallStreamEvent]] = []

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        self.published.append((room_name, event))


@pytest.mark.asyncio
async def test_publish_field_answer_envelope() -> None:
    store = _SpyStore()
    service = CallStreamService(store)  # type: ignore[arg-type]
    dispute = {
        "previous_value": "John Doe",
        "current_value": "Jane Doe",
        "confidence": 88,
        "evidence": None,
        "reasoning": None,
    }
    await service.publish_field_answer(
        "room-1",
        field_path="sections.patient.name",
        value="Jane Doe",
        confidence=88,
        evidence_seq=12,
        completion_pct=40,
        dispute=dispute,
        ts=99,
    )
    [(room, event)] = store.published
    assert room == "room-1"
    assert event.type == TYPE_FIELD_ANSWER == "field_answer"
    assert event.data == {
        "field_path": "sections.patient.name",
        "value": "Jane Doe",
        "source": "ai_call",
        "confidence": 88,
        "evidence_seq": 12,
        "completion_pct": 40,
        "dispute": dispute,
    }
    assert event.ts == 99


@pytest.mark.asyncio
async def test_publish_field_answer_allows_nullable_metadata() -> None:
    store = _SpyStore()
    service = CallStreamService(store)  # type: ignore[arg-type]
    await service.publish_field_answer(
        "room-2",
        field_path="sections.patient.dob",
        value="1990-01-01",
        confidence=None,
        evidence_seq=None,
        completion_pct=None,
        dispute=None,
        ts=1,
    )
    [(_, event)] = store.published
    assert event.data["confidence"] is None
    assert event.data["evidence_seq"] is None
    assert event.data["completion_pct"] is None
    assert event.data["dispute"] is None
