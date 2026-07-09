"""Verifier state machine: pending → available (sha256 stamped) / failed /
discarded (no-answer). Uses fakes for gateway+storage and a stub work-list."""

import hashlib
import time
from typing import Any
from uuid import uuid4

import pytest

from control_plane.livekit_gateway import ActiveEgress, EgressState
from control_plane.recording_jobs import PendingRow, RecordingVerifier
from control_plane.recording_storage import InMemoryRecordingStorage
from vera_core.models.audit_log import AuditEvent
from vera_core.models.enums import RecordingStatus
from vera_core.observability.correlation import room_name_for_call
from vera_core.services.recordings import RecordingConfig, recording_object_path


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


# --- orphan-egress reconciliation ------------------------------------------
#
# An egress starts BEFORE its PENDING Recording row commits (services/recordings.py),
# so if the caller transaction rolls back after egress start, audio keeps uploading
# to GCS with no row — and nothing else ever verifies or retention-deletes it. The
# verifier reaps these: an active egress in a vera room, older than the grace window,
# with no PENDING row → stop it + delete the partial object + audit.

_CONFIG = RecordingConfig(bucket="bkt", prefix="recordings")
_ANCIENT_MS = 1_000  # ~1970 — always older than the grace window


class _ReconcileGateway:
    def __init__(self, active: list[ActiveEgress]) -> None:
        self._active = active
        self.stopped: list[str] = []

    async def list_active_egresses(self) -> list[ActiveEgress]:
        return self._active

    async def stop_egress(self, egress_id: str) -> None:
        self.stopped.append(egress_id)


def _reconcile_verifier(
    gateway: _ReconcileGateway, storage: InMemoryRecordingStorage, audit: _FakeAudit
) -> RecordingVerifier:
    return RecordingVerifier(
        sessionmaker=object(),  # type: ignore[arg-type]  # unused by _reconcile_orphans
        livekit=gateway,  # type: ignore[arg-type]
        storage=storage,
        audit=audit,
        interval_seconds=30,
        retention_days_default=90,
        recording_config=_CONFIG,
        orphan_grace_seconds=300,
    )


async def test_reconcile_reaps_orphan_egress_with_no_row() -> None:
    tenant_id, call_id = uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    object_path = recording_object_path(_CONFIG, tenant_id, call_id)
    storage = InMemoryRecordingStorage()
    storage.objects[("bkt", object_path)] = b"partial-orphan-audio"
    gateway = _ReconcileGateway([ActiveEgress("EG_ORPHAN", room, _ANCIENT_MS)])
    audit = _FakeAudit()
    verifier = _reconcile_verifier(gateway, storage, audit)

    await verifier._reconcile_orphans([])  # no pending rows → egress is orphaned

    assert gateway.stopped == ["EG_ORPHAN"]
    assert ("bkt", object_path) not in storage.objects  # partial object deleted
    assert audit.records[0].event_type == AuditEvent.RECORDING_DISCARDED.value
    assert audit.records[0].detail["reason"] == "orphaned_egress"


async def test_reconcile_leaves_egress_that_has_a_pending_row() -> None:
    tenant_id, call_id = uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    gateway = _ReconcileGateway([ActiveEgress("EG_LIVE", room, _ANCIENT_MS)])
    verifier = _reconcile_verifier(gateway, InMemoryRecordingStorage(), _FakeAudit())

    known = [
        PendingRow(
            tenant_id=tenant_id,
            recording_id=uuid4(),
            call_id=call_id,
            egress_id="EG_LIVE",
            gcs_uri="gs://bkt/x.ogg",
        )
    ]
    await verifier._reconcile_orphans(known)
    assert gateway.stopped == []  # a tracked egress is never reaped


async def test_reconcile_spares_recently_started_egress_within_grace() -> None:
    tenant_id, call_id = uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    just_now = int(time.time() * 1000)
    gateway = _ReconcileGateway([ActiveEgress("EG_NEW", room, just_now)])
    verifier = _reconcile_verifier(gateway, InMemoryRecordingStorage(), _FakeAudit())

    # Its row may still be committing — the grace window prevents killing a
    # legitimate just-started recording.
    await verifier._reconcile_orphans([])
    assert gateway.stopped == []


async def test_reconcile_ignores_foreign_non_vera_room() -> None:
    gateway = _ReconcileGateway([ActiveEgress("EG_FOREIGN", "some-other-room", _ANCIENT_MS)])
    verifier = _reconcile_verifier(gateway, InMemoryRecordingStorage(), _FakeAudit())
    await verifier._reconcile_orphans([])
    assert gateway.stopped == []  # not ours — never touch a non-call room
