"""Verifier state machine: pending → available (sha256 stamped) / failed /
discarded (no-answer). Uses fakes for gateway+storage and a stub work-list."""

import hashlib
from typing import Any
from uuid import uuid4

import pytest

from control_plane.livekit_gateway import EgressState
from control_plane.recording_jobs import PendingRow, RecordingVerifier
from control_plane.recording_storage import InMemoryRecordingStorage
from vera_core.models.enums import RecordingStatus


class _FakeGateway:
    def __init__(self, state: EgressState | None) -> None:
        self._state = state

    async def get_egress_status(self, egress_id: str) -> EgressState | None:
        return self._state


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def emit(self, record: Any) -> None:
        self.records.append(record)


@pytest.fixture
def row() -> PendingRow:
    return PendingRow(
        tenant_id=uuid4(),
        recording_id=uuid4(),
        call_id=uuid4(),
        egress_id="EG_1",
        gcs_uri="gs://bkt/recordings/t/c.ogg",
    )


def _verifier(
    gateway: _FakeGateway,
    storage: InMemoryRecordingStorage,
    audit: _FakeAudit,
    monkeypatch: pytest.MonkeyPatch,
    *,
    call_status: str = "completed",
    updates: list[dict[str, Any]],
) -> RecordingVerifier:
    """Build a verifier with the DB seams stubbed out: _apply_update captures the
    payload; the call/tenant lookups are monkeypatched to canned values."""
    verifier = RecordingVerifier(
        sessionmaker=object(),  # type: ignore[arg-type]  # DB seams stubbed below
        livekit=gateway,  # type: ignore[arg-type]
        storage=storage,
        audit=audit,
        interval_seconds=30,
        retention_days_default=90,
    )

    async def _capture(row: PendingRow, *, expected: str, values: dict[str, Any]) -> None:
        updates.append({"expected": expected, **values})

    monkeypatch.setattr(verifier, "_apply_update", _capture)
    # _load_call / _load_retention_days are the verifier's deliberate DB seams
    # (see Step 3) — stub them so no sessionmaker is needed.
    from types import SimpleNamespace

    async def _load_call(row: PendingRow) -> Any:
        return SimpleNamespace(current_status=call_status, ended_at=None)

    async def _load_retention_days(row: PendingRow) -> int:
        return 90

    monkeypatch.setattr(verifier, "_load_call", _load_call)
    monkeypatch.setattr(verifier, "_load_retention_days", _load_retention_days)
    return verifier


async def test_in_progress_egress_applies_nothing(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    verifier = _verifier(
        _FakeGateway(EgressState(complete=False, failed=False, duration_ms=None, size_bytes=None)),
        InMemoryRecordingStorage(),
        _FakeAudit(),
        monkeypatch,
        updates=updates,
    )
    await verifier._verify_one(row)
    assert updates == []


async def test_complete_egress_verifies_sha256_and_stamps_retention(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    storage = InMemoryRecordingStorage()
    body = b"ogg-bytes"
    storage.objects[("bkt", "recordings/t/c.ogg")] = body
    verifier = _verifier(
        _FakeGateway(EgressState(complete=True, failed=False, duration_ms=90_000, size_bytes=9)),
        storage,
        _FakeAudit(),
        monkeypatch,
        updates=updates,
    )
    await verifier._verify_one(row)
    (update,) = updates
    assert update["expected"] == RecordingStatus.PENDING.value
    assert update["status"] == RecordingStatus.AVAILABLE.value
    assert update["sha256"] == hashlib.sha256(body).hexdigest()
    assert update["size_bytes"] == len(body)
    assert update["duration_ms"] == 90_000
    assert update["retention_until"] is not None


async def test_no_answer_call_discards_object(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    storage = InMemoryRecordingStorage()
    storage.objects[("bkt", "recordings/t/c.ogg")] = b"x"
    verifier = _verifier(
        _FakeGateway(EgressState(complete=True, failed=False, duration_ms=1, size_bytes=1)),
        storage,
        _FakeAudit(),
        monkeypatch,
        call_status="no_answer",
        updates=updates,
    )
    await verifier._verify_one(row)
    assert not await storage.exists("bkt", "recordings/t/c.ogg")
    assert updates[0]["status"] == RecordingStatus.DISCARDED.value


async def test_lost_or_failed_egress_marks_failed_and_audits(
    row: PendingRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates: list[dict[str, Any]] = []
    audit = _FakeAudit()
    verifier = _verifier(
        _FakeGateway(None), InMemoryRecordingStorage(), audit, monkeypatch, updates=updates
    )
    await verifier._verify_one(row)
    assert updates[0]["status"] == RecordingStatus.FAILED.value
    assert audit.records[0].event_type == "recording.failed"
