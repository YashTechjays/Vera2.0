"""Pipeline sweeper — the time-based safety net for the call pipeline.

The pipeline is event-driven (enqueue → dispatch; worker events → closeout →
refill), which leaves two timing holes this loop closes on every tick:

1. RECONCILE stuck calls. A hard-crashed worker never emits `call.ended`, so its
   form would sit IN_CALL forever and leak a concurrency slot. Signal: the healthy
   end path always deletes the LiveKit room (delete_room_on_close / the consumer's
   call.failed teardown), so a non-terminal Call whose room is GONE — past a grace
   window — is dead; it is failed through the same `close_call` path the consumer
   uses (bounded auto-retry, audit). A non-terminal call past the hard duration cap
   gets its room deleted first (ends a wedged session), then the same closeout.
2. WAKE the dispatcher. Queued forms whose blocking condition lapsed (working
   hours reopened, a slot freed by reconciliation) get a dispatch pass without
   waiting for the next enqueue/call-end event; queue expiry rides the same pass
   (try_dispatch expires stale forms).

Tenant enumeration runs under platform_session (the tenant catalog is
platform-readable — migration 0022); every row mutation runs per-tenant under
tenant_session, so RLS isolation is never bypassed. Concurrent sweepers (multiple
control-plane replicas) are safe: close_call re-checks terminal state under a row
lock, so the loser of a race is a no-op. No PHI flows here — ids, room names,
statuses, and counts only.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.call_closeout import TERMINAL_VALUES, close_call
from control_plane.dispatch import run_dispatch_pass
from control_plane.post_call import sweep_stuck_ai_processing
from control_plane.transcript_finalizer import finalize_transcript
from vera_core.audit import AuditSink
from vera_core.call_stream import CallStreamService
from vera_core.db.rls import platform_session, tenant_session
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.enums import CallStatus, FormStatus
from vera_core.observability.correlation import is_observer_identity, room_name_for_call

logger = logging.getLogger("control_plane.pipeline_sweeper")


def rooms_to_close(
    rows: list[tuple[UUID, bool, bool]],
    live_rooms: set[str],
    observer_only_rooms: set[str],
    tenant_id: UUID,
) -> list[tuple[str, bool, CallStatus]]:
    """Which stuck-call candidates to close: `(room_name, delete_room_first, status)`.

    rows: (call_id, past_cap, end_requested) for non-terminal calls past the
    grace window. Room gone → close (the room needs no delete). Room live but
    past the hard cap, or held open only by browser observers (no agent, no SIP
    callee — the call can never progress) → delete the room first, then close.
    Room live and within the cap with a real participant → a long call still in
    progress; leave it alone. A call whose end was user-requested closes as
    CANCELED (never auto-redialed); everything else as FAILED.
    """
    result: list[tuple[str, bool, CallStatus]] = []
    for call_id, past_cap, end_requested in rows:
        room_name = room_name_for_call(tenant_id, call_id)
        status = CallStatus.CANCELED if end_requested else CallStatus.FAILED
        if room_name not in live_rooms:
            result.append((room_name, False, status))
        elif past_cap or room_name in observer_only_rooms:
            result.append((room_name, True, status))
    return result


class PipelineSweeper:
    """Periodic reconcile-and-dispatch loop; one per control-plane process."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        livekit: Any,
        kms: Any,
        audit: AuditSink,
        call_stream: CallStreamService,
        *,
        interval_s: float,
        stuck_grace_s: int,
        max_call_duration_s: int,
        form_auto_retry_enabled: bool = False,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._livekit = livekit
        self._kms = kms
        self._audit = audit
        self._call_stream = call_stream
        self._interval_s = interval_s
        self._stuck_grace_s = stuck_grace_s
        self._max_call_duration_s = max_call_duration_s
        self._form_auto_retry_enabled = form_auto_retry_enabled

    async def run(self) -> None:
        """Sweep immediately on boot, then every interval. Mirrors the worker-event
        consumer's resilience: any error logs and waits for the next tick."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pipeline sweep failed; retrying next interval")
            await asyncio.sleep(self._interval_s)

    async def sweep_once(self) -> None:
        async with platform_session(self._sessionmaker) as session:
            tenant_ids = list((await session.execute(select(Tenant.id))).scalars().all())
        for tenant_id in tenant_ids:
            try:
                await self._sweep_tenant(tenant_id)
            except Exception:  # one tenant's failure must not starve the rest
                logger.exception("sweep failed for tenant %s; continuing", tenant_id)

    async def _sweep_tenant(self, tenant_id: UUID) -> None:
        # Phase 1 (read-only, lock-free, DB-clock interval math): stuck-call
        # candidates + whether any dispatchable work is queued.
        # func.make_interval args are positional (years, months, weeks, days,
        # hours, mins, secs) — seconds is the 7th.
        grace = func.make_interval(0, 0, 0, 0, 0, 0, self._stuck_grace_s)
        cap = func.make_interval(0, 0, 0, 0, 0, 0, self._max_call_duration_s)
        async with tenant_session(self._sessionmaker, tenant_id) as session:
            stuck_candidates = await session.execute(
                select(
                    Call.id,
                    (Call.created_at < func.now() - cap).label("past_cap"),
                    Call.end_requested_by_id.is_not(None).label("end_requested"),
                ).where(
                    Call.tenant_id == tenant_id,
                    Call.current_status.not_in(list(TERMINAL_VALUES)),
                    Call.created_at < func.now() - grace,
                )
            )
            rows = [(row.id, row.past_cap, row.end_requested) for row in stuck_candidates.all()]
            has_queued = (
                await session.execute(
                    select(PatientForm.id)
                    .where(PatientForm.status == FormStatus.IN_QUEUE.value)
                    .limit(1)
                )
            ).scalar_one_or_none() is not None

        # Phase 2: probe LiveKit once, close the dead ones via the shared path.
        closed = 0
        if rows:
            candidate_rooms = [room_name_for_call(tenant_id, cid) for cid, _, _ in rows]
            live_rooms = await self._livekit.existing_rooms(candidate_rooms)
            observer_only: set[str] = set()
            for room_name in sorted(live_rooms):
                identities = await self._livekit.room_participant_identities(room_name)
                if identities is None:
                    live_rooms.discard(room_name)  # vanished between the two probes
                elif all(is_observer_identity(i) for i in identities):
                    # empty, or only supervisors/monitors — nothing can progress
                    observer_only.add(room_name)
            for room_name, delete_first, status in rooms_to_close(
                rows, live_rooms, observer_only, tenant_id
            ):
                if delete_first:
                    logger.warning(
                        "sweeper: room %s is dead (past cap or observer-only); deleting", room_name
                    )
                    await self._livekit.delete_room(room_name)
                ref = await close_call(
                    self._sessionmaker,
                    self._audit,
                    room_name,
                    status,
                    trigger="sweeper_reconcile",
                    actor_label="pipeline-sweeper",
                )
                if ref is not None:
                    await finalize_transcript(self._sessionmaker, self._call_stream, ref, room_name)
                    closed += 1
                    logger.info(
                        "sweeper: reconciled stuck call room %s as %s", room_name, status.value
                    )

        # Phase 3: resolve forms stranded in AI_PROCESSING (a crash between
        # closeout and post-call resolution leaks a concurrency slot forever —
        # the dispatcher counts AI_PROCESSING as active).
        resolved = await sweep_stuck_ai_processing(
            self._sessionmaker,
            self._audit,
            tenant_id,
            grace_s=self._stuck_grace_s,
            auto_retry_enabled=self._form_auto_retry_enabled,
        )

        # Phase 4: time-based dispatch wake-up — freed slots and/or queued forms.
        if closed or resolved or has_queued:
            await run_dispatch_pass(
                self._sessionmaker, tenant_id, self._livekit, self._kms, self._audit
            )
