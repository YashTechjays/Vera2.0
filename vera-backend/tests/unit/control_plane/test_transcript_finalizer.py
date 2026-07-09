"""call.ended → drain the stream → idempotent transcript insert (unit level:
assert the row payloads the handler builds; DB idempotency is the ON CONFLICT
clause, exercised in the integration suite)."""

from datetime import datetime
from uuid import uuid4

from control_plane.worker_events import WorkerEventConsumer, build_transcript_rows
from vera_core.models.enums import TranscriptSource
from vera_core.transcript import InMemoryTranscriptStore, TranscriptEvent, TranscriptService


def test_build_transcript_rows_maps_roles_and_seq() -> None:
    tenant_id, call_id = uuid4(), uuid4()
    events = [
        TranscriptEvent(role="user", text="[[NAME_1]] speaking", ts=1_700_000_000_000),
        TranscriptEvent(role="agent", text="hello [[NAME_1]]", ts=1_700_000_001_000),
    ]
    rows = build_transcript_rows(tenant_id, call_id, events)
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[0]["source"] == TranscriptSource.REP.value
    assert rows[1]["source"] == TranscriptSource.BOT.value
    assert rows[0]["message"] == "[[NAME_1]] speaking"
    assert rows[0]["tenant_id"] == tenant_id and rows[0]["call_id"] == call_id
    spoke_at = rows[0]["spoke_at"]
    assert isinstance(spoke_at, datetime)
    assert spoke_at.timestamp() == 1_700_000_000.0
    assert all("id" in r for r in rows)  # bulk insert bypasses the ORM client default


async def test_handler_registered_only_with_deps() -> None:
    class _R:  # never touched in this test
        pass

    bare = WorkerEventConsumer(_R(), livekit=None)  # type: ignore[arg-type]
    assert "call.ended" not in bare._handlers
    wired = WorkerEventConsumer(
        _R(),  # type: ignore[arg-type]
        livekit=None,  # type: ignore[arg-type]
        sessionmaker=object(),  # type: ignore[arg-type]
        transcripts=TranscriptService(InMemoryTranscriptStore()),
    )
    assert "call.ended" in wired._handlers
