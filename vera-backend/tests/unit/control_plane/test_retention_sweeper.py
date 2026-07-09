"""Retention sweeper: before/after audited deletion past retention_until.

Uses fakes for storage+audit and monkeypatched DB seams (_load_available /
_apply_tombstone) — no sessionmaker needed. Mirrors the Task 8 verifier test style.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.recording_jobs import RetentionSweeper
from control_plane.recording_storage import InMemoryRecordingStorage


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def emit(self, record: Any) -> None:
        self.records.append(record)


class _StickyInMemoryStorage(InMemoryRecordingStorage):
    """delete() is a no-op — simulates a GCS failure or eventual-consistency gap."""

    async def delete(self, bucket: str, object_path: str) -> None:
        pass  # intentionally leave the object in place


def _fake_rec(
    recording_id: UUID,
    *,
    call_id: UUID,
    gcs_uri: str = "gs://bkt/recordings/t/c.ogg",
    sha256: str = "deadbeef" * 8,
    size_bytes: int = 42,
    retention_until: datetime = datetime(2025, 1, 1, tzinfo=UTC),
) -> Any:
    return SimpleNamespace(
        id=recording_id,
        call_id=call_id,
        gcs_uri=gcs_uri,
        sha256=sha256,
        size_bytes=size_bytes,
        retention_until=retention_until,
    )


def _sweeper(
    storage: InMemoryRecordingStorage,
    audit: _FakeAudit,
    monkeypatch: pytest.MonkeyPatch,
    *,
    available_rec: Any | None,
    tombstones: list[dict[str, UUID]],
) -> RetentionSweeper:
    """Build a sweeper with DB seams stubbed: _load_available returns a canned row
    (or None); _apply_tombstone captures calls without touching the DB."""
    sweeper = RetentionSweeper(
        sessionmaker=object(),  # type: ignore[arg-type]  # DB seams stubbed below
        storage=storage,
        audit=audit,
        interval_seconds=3600,
    )

    async def _load_available(tenant_id: UUID, recording_id: UUID) -> Any | None:
        return available_rec

    async def _apply_tombstone(tenant_id: UUID, recording_id: UUID) -> None:
        tombstones.append({"tenant_id": tenant_id, "recording_id": recording_id})

    monkeypatch.setattr(sweeper, "_load_available", _load_available)
    monkeypatch.setattr(sweeper, "_apply_tombstone", _apply_tombstone)
    return sweeper


async def test_due_row_deletes_object_and_emits_before_after_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: before-audit → delete → after-audit → tombstone."""
    tenant_id = uuid4()
    recording_id = uuid4()
    call_id = uuid4()

    storage = InMemoryRecordingStorage()
    storage.objects[("bkt", "recordings/t/c.ogg")] = b"audio-bytes"
    audit = _FakeAudit()
    tombstones: list[dict[str, UUID]] = []
    rec = _fake_rec(recording_id, call_id=call_id)

    sweeper = _sweeper(storage, audit, monkeypatch, available_rec=rec, tombstones=tombstones)
    await sweeper._sweep_one(tenant_id, recording_id)

    # Object must be gone from storage.
    assert not await storage.exists("bkt", "recordings/t/c.ogg")

    # Two audit records: before, then after.
    assert len(audit.records) == 2
    before_rec, after_rec = audit.records

    assert before_rec.event_type == "recording.deleted"
    assert before_rec.detail["phase"] == "before"
    assert before_rec.detail["gcs_uri"] == rec.gcs_uri
    assert before_rec.detail["sha256"] == rec.sha256
    assert before_rec.detail["size_bytes"] == rec.size_bytes
    assert before_rec.detail["retention_until"] == rec.retention_until.isoformat()
    assert before_rec.detail["call_id"] == str(call_id)

    assert after_rec.event_type == "recording.deleted"
    assert after_rec.detail["phase"] == "after"
    assert after_rec.detail["verified_gone"] is True
    assert after_rec.detail["call_id"] == str(call_id)

    # Tombstone must be applied exactly once with correct ids.
    assert len(tombstones) == 1
    assert tombstones[0]["tenant_id"] == tenant_id
    assert tombstones[0]["recording_id"] == recording_id


async def test_storage_delete_noop_skips_after_audit_and_tombstone(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the object persists after delete(), emit only before-audit and log an error;
    no after-audit and no tombstone (so the next tick retries)."""
    tenant_id = uuid4()
    recording_id = uuid4()
    call_id = uuid4()

    storage = _StickyInMemoryStorage()
    storage.objects[("bkt", "recordings/t/c.ogg")] = b"audio-bytes"
    audit = _FakeAudit()
    tombstones: list[dict[str, UUID]] = []
    rec = _fake_rec(recording_id, call_id=call_id)

    sweeper = _sweeper(storage, audit, monkeypatch, available_rec=rec, tombstones=tombstones)

    with caplog.at_level(logging.ERROR, logger="control_plane.recording_jobs"):
        await sweeper._sweep_one(tenant_id, recording_id)

    # Only the before-audit; no after.
    assert len(audit.records) == 1
    assert audit.records[0].detail["phase"] == "before"

    # No tombstone — will be retried next tick.
    assert tombstones == []

    # Error must be logged.
    assert any("still present" in r.message for r in caplog.records)


async def test_row_not_available_skips_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _load_available returns None (already swept by a replica or status changed),
    nothing is emitted and storage is untouched."""
    tenant_id = uuid4()
    recording_id = uuid4()

    storage = InMemoryRecordingStorage()
    audit = _FakeAudit()
    tombstones: list[dict[str, UUID]] = []

    sweeper = _sweeper(storage, audit, monkeypatch, available_rec=None, tombstones=tombstones)
    await sweeper._sweep_one(tenant_id, recording_id)

    assert audit.records == []
    assert tombstones == []
    assert storage.objects == {}
