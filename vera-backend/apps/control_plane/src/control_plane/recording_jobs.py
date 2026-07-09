"""Recording background jobs: the egress-reconciliation verifier and the
retention sweeper. Both are control-plane lifespan tasks following the
WorkerEventConsumer loop discipline (never die: log + sleep on error).

Cross-tenant discovery goes through SECURITY DEFINER work-list functions
(recording_pending_work / recording_retention_due — ids and non-PHI pointers
only); every row mutation runs inside tenant_session(...) with full RLS.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.livekit_gateway import LiveKitGateway
from control_plane.recording_storage import RecordingStorage, parse_gcs_uri
from vera_core.audit import AuditRecord, AuditSink
from vera_core.db import tenant_session
from vera_core.models import Call, Recording, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallStatus, RecordingStatus

logger = logging.getLogger("control_plane.recording_jobs")

_DISCARD_CALL_STATUSES = frozenset({CallStatus.NO_ANSWER, CallStatus.BUSY})


async def _run_forever(
    label: str, tick: Callable[[], Awaitable[None]], interval_seconds: int
) -> None:
    """Never-die loop discipline shared by both jobs (WorkerEventConsumer style):
    an uncaught tick error is logged and the loop sleeps on; only cancellation exits."""
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s tick failed; continuing", label)
        await asyncio.sleep(interval_seconds)


async def _guarded_recording_update(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    recording_id: UUID,
    *,
    expected: str,
    values: dict[str, Any],
) -> None:
    """State-guarded UPDATE: a replica that already transitioned the row wins; ours no-ops."""
    async with tenant_session(sessionmaker, tenant_id) as session:
        await session.execute(
            update(Recording)
            .where(Recording.id == recording_id, Recording.status == expected)
            .values(**values)
        )


@dataclass(frozen=True)
class PendingRow:
    tenant_id: UUID
    recording_id: UUID
    call_id: UUID
    egress_id: str | None
    gcs_uri: str


class RecordingVerifier:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        livekit: LiveKitGateway,
        storage: RecordingStorage,
        audit: AuditSink,
        *,
        interval_seconds: int,
        retention_days_default: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._livekit = livekit
        self._storage = storage
        self._audit = audit
        self._interval = interval_seconds
        self._retention_days_default = retention_days_default

    async def run(self) -> None:
        await _run_forever("recording verifier", self.tick, self._interval)

    async def tick(self) -> None:
        rows = await self._pending_rows()
        for row in rows:
            try:
                await self._verify_one(row)
            except Exception:
                # One bad row must not starve the rest; state-guarded updates make
                # a retry next tick safe.
                logger.exception("verify failed for recording %s", row.recording_id)

    async def _pending_rows(self) -> list[PendingRow]:
        async with self._sessionmaker() as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id, recording_id, call_id, egress_id, gcs_uri"
                    " FROM recording_pending_work()"
                )
            )
            return [PendingRow(*row) for row in result.all()]

    async def _verify_one(self, row: PendingRow) -> None:
        if row.egress_id is None:  # FAILED-at-start rows never enter pending; guard anyway
            return
        state = await self._livekit.get_egress_status(row.egress_id)
        if state is None or state.failed:
            await self._mark_failed(row, reason="egress_lost" if state is None else "egress_failed")
            return
        if not state.complete:
            return  # still recording — next tick

        bucket, object_path = parse_gcs_uri(row.gcs_uri)
        call = await self._load_call(row)

        if call is not None and call.current_status in _DISCARD_CALL_STATUSES:
            # No-answer/busy: nothing worth keeping — delete the object now.
            await self._storage.delete(bucket, object_path)
            await self._apply_update(
                row,
                expected=RecordingStatus.PENDING.value,
                values={
                    "status": RecordingStatus.DISCARDED.value,
                    "deleted_at": func.now(),
                },
            )
            await self._emit(
                row, AuditEvent.RECORDING_DISCARDED, {"call_status": call.current_status}
            )
            return

        if call is None:
            logger.warning(
                "recording %s: call %s not found during verification; proceeding to sha256",
                row.recording_id,
                row.call_id,
            )

        digest = await self._storage.sha256_and_size(bucket, object_path)
        if digest is None:
            return  # upload not visible in GCS yet — retry next tick
        sha256, size_bytes = digest

        days = await self._load_retention_days(row)
        ended_at = call.ended_at if call is not None else None
        retention_until = (
            ended_at + timedelta(days=days)
            if ended_at is not None
            else func.now() + func.make_interval(0, 0, 0, days)  # DB clock, not app clock
        )
        await self._apply_update(
            row,
            expected=RecordingStatus.PENDING.value,
            values={
                "status": RecordingStatus.AVAILABLE.value,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "duration_ms": state.duration_ms,
                "retention_until": retention_until,
            },
        )
        logger.info("recording %s verified (sha256=%s…)", row.recording_id, sha256[:12])

    async def _load_call(self, row: PendingRow) -> Call | None:
        """Small seam so unit tests can stub the DB lookup."""
        async with tenant_session(self._sessionmaker, row.tenant_id) as session:
            return (
                await session.execute(select(Call).where(Call.id == row.call_id))
            ).scalar_one_or_none()

    async def _load_retention_days(self, row: PendingRow) -> int:
        """Tenant override or the platform default (small seam, see _load_call)."""
        async with tenant_session(self._sessionmaker, row.tenant_id) as session:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.id == row.tenant_id))
            ).scalar_one_or_none()
        if tenant is not None and tenant.recording_retention_days is not None:
            return tenant.recording_retention_days
        return self._retention_days_default

    async def _mark_failed(self, row: PendingRow, *, reason: str) -> None:
        await self._apply_update(
            row,
            expected=RecordingStatus.PENDING.value,
            values={"status": RecordingStatus.FAILED.value},
        )
        await self._emit(row, AuditEvent.RECORDING_FAILED, {"reason": reason})

    async def _apply_update(
        self, row: PendingRow, *, expected: str, values: dict[str, Any]
    ) -> None:
        await _guarded_recording_update(
            self._sessionmaker, row.tenant_id, row.recording_id, expected=expected, values=values
        )

    async def _emit(self, row: PendingRow, event: AuditEvent, detail: dict[str, Any]) -> None:
        await self._audit.emit(
            AuditRecord(
                tenant_id=row.tenant_id,
                actor_type=ActorType.SYSTEM,
                actor_label="recording-verifier",
                event_type=event.value,
                resource_type="recording",
                resource_id=str(row.recording_id),
                detail={"call_id": str(row.call_id), **detail},
            )
        )


class RetentionSweeper:
    """Deletes recordings past retention_until with before/after audit snapshots
    (spec decision 5). GCS delete is idempotent (absent → no-op) and the tombstone
    update is state-guarded, so replicas and retries are safe."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: RecordingStorage,
        audit: AuditSink,
        *,
        interval_seconds: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._audit = audit
        self._interval = interval_seconds

    async def run(self) -> None:
        await _run_forever("retention sweep", self.tick, self._interval)

    async def tick(self) -> None:
        async with self._sessionmaker() as session:
            result = await session.execute(
                text("SELECT tenant_id, recording_id FROM recording_retention_due()")
            )
            due = [(UUID(str(t)), UUID(str(r))) for t, r in result.all()]
        for tenant_id, recording_id in due:
            try:
                await self._sweep_one(tenant_id, recording_id)
            except Exception:
                # One bad row must not starve the rest; the delete is idempotent and
                # the tombstone is state-guarded, so a retry next tick is safe.
                logger.exception("sweep failed for recording %s", recording_id)

    async def _sweep_one(self, tenant_id: UUID, recording_id: UUID) -> None:
        rec = await self._load_available(tenant_id, recording_id)
        if rec is None:
            return  # already swept (replica) or state changed — nothing to do

        # BEFORE snapshot: evidence survives in the append-only audit_log even if
        # we crash mid-delete, so recovery can confirm what was destroyed.
        await self._emit_deleted(
            tenant_id,
            recording_id,
            call_id=rec.call_id,
            detail={
                "phase": "before",
                "gcs_uri": rec.gcs_uri,
                "size_bytes": rec.size_bytes,
                "sha256": rec.sha256,
                "retention_until": rec.retention_until.isoformat() if rec.retention_until else None,
            },
        )
        bucket, object_path = parse_gcs_uri(rec.gcs_uri)
        await self._storage.delete(bucket, object_path)
        if await self._storage.exists(bucket, object_path):
            logger.error("recording %s object still present after delete; will retry", recording_id)
            return  # no AFTER record, no tombstone — retried next tick

        await self._apply_tombstone(tenant_id, recording_id)
        await self._emit_deleted(
            tenant_id,
            recording_id,
            call_id=rec.call_id,
            detail={"phase": "after", "verified_gone": True},
        )

    async def _load_available(self, tenant_id: UUID, recording_id: UUID) -> Recording | None:
        """Seam: load the recording only if still status=AVAILABLE (stub in tests).

        Returns the ORM row for use after the session closes; safe because the
        shared sessionmaker sets expire_on_commit=False (db/engine.py), so the
        detached instance keeps its loaded attributes (gcs_uri, sha256, ...).
        """
        async with tenant_session(self._sessionmaker, tenant_id) as session:
            return (
                await session.execute(
                    select(Recording).where(
                        Recording.id == recording_id,
                        Recording.status == RecordingStatus.AVAILABLE.value,
                    )
                )
            ).scalar_one_or_none()

    async def _apply_tombstone(self, tenant_id: UUID, recording_id: UUID) -> None:
        """Seam: AVAILABLE → DELETED tombstone; sha256/size evidence columns retained."""
        await _guarded_recording_update(
            self._sessionmaker,
            tenant_id,
            recording_id,
            expected=RecordingStatus.AVAILABLE.value,
            values={"status": RecordingStatus.DELETED.value, "deleted_at": func.now()},
        )

    async def _emit_deleted(
        self, tenant_id: UUID, recording_id: UUID, *, call_id: UUID, detail: dict[str, Any]
    ) -> None:
        await self._audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.SYSTEM,
                actor_label="retention-sweeper",
                event_type=AuditEvent.RECORDING_DELETED.value,
                resource_type="recording",
                resource_id=str(recording_id),
                detail={"call_id": str(call_id), **detail},
            )
        )
