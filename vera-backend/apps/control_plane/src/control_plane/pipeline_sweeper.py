"""Pipeline sweeper — the time-based safety net for the call pipeline.

The pipeline is event-driven (enqueue → dispatch; worker events → closeout →
refill), which leaves two timing holes this loop closes on every tick:

1. RECONCILE stuck calls. A hard-crashed worker never emits `call.ended`, so its
   form would sit IN_CALL forever and leak a concurrency slot. Signal: the healthy
   end path always deletes the LiveKit room (delete_room_on_close / the consumer's
   call.failed teardown), so a non-terminal Call whose room is GONE — on two
   consecutive ticks — is dead; it is failed through the same `close_call` path the
   consumer uses (bounded auto-retry, audit). The two-tick confirmation exists
   because room-gone is also the healthy closeout's transient state: the worker
   deletes the room moments before the consumer's `close_call` commits, and a
   single-sighting sweep in that window would misclassify a normally completed
   call as FAILED and auto-redial the payer (the created_at grace window gives no
   protection at end-of-call). A non-terminal call past the hard duration cap
   gets its room deleted first (ends a wedged session), then the same closeout.
2. WAKE the dispatcher. Queued forms whose blocking condition lapsed (working
   hours reopened, a slot freed by reconciliation) get a dispatch pass without
   waiting for the next enqueue/call-end event; queue expiry rides the same pass
   (stage_dispatch expires stale forms).

Tenant enumeration runs under platform_session (the tenant catalog is
platform-readable — migration 0022); every row mutation runs per-tenant under
tenant_session, so RLS isolation is never bypassed. Concurrent sweepers (multiple
control-plane replicas) are safe: close_call re-checks terminal state under a row
lock, so the loser of a race is a no-op. No PHI flows here — ids, room names,
statuses, and counts only.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.call_closeout import TERMINAL_VALUES, announce_terminal_status, close_call
from control_plane.dispatch import run_dispatch_pass
from control_plane.post_call import resolve_ai_processing, sweep_stuck_ai_processing
from control_plane.transcript_finalizer import finalize_transcript
from vera_core.audit import AuditSink
from vera_core.call_stream import CallStreamService
from vera_core.db.rls import platform_session, tenant_session
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.enums import CallStatus, FormStatus
from vera_core.observability.correlation import (
    is_observer_identity,
    parse_room_name,
    room_name_for_call,
)
from vera_core.plan_store import CallPlanService

if TYPE_CHECKING:
    from vera_core.services.recordings import RecordingConfig

logger = logging.getLogger("control_plane.pipeline_sweeper")


def rooms_to_close(
    rows: list[tuple[UUID, bool, bool]],
    live_rooms: set[str],
    observer_only_rooms: set[str],
    tenant_id: UUID,
    *,
    confirmed_gone: set[str],
) -> tuple[list[tuple[str, bool, CallStatus]], set[str]]:
    """Which stuck-call candidates to close: `(to_close, newly_gone)` where
    to_close is `[(room_name, delete_room_first, status), ...]`.

    rows: (call_id, past_cap, end_requested) for non-terminal calls past the
    grace window. Room gone → close (no room left to delete), but only when it
    was ALSO gone on the previous tick (`confirmed_gone`); a first sighting is
    deferred into `newly_gone` — the healthy closeout deletes the room moments
    before `close_call` commits, so one tick of patience keeps the sweeper from
    misclassifying a normally completed call as FAILED (→ auto-redial). Room
    live but past the hard cap, or held open only by browser observers (no
    agent, no SIP callee — the call can never progress) → delete the room
    first, then close. Room live and within the cap with a real participant →
    a long call still in progress; leave it alone. A call whose end was
    user-requested closes as CANCELED (never auto-redialed); everything else
    as FAILED.
    """
    result: list[tuple[str, bool, CallStatus]] = []
    newly_gone: set[str] = set()
    for call_id, past_cap, end_requested in rows:
        room_name = room_name_for_call(tenant_id, call_id)
        status = CallStatus.CANCELED if end_requested else CallStatus.FAILED
        if room_name not in live_rooms:
            if room_name in confirmed_gone:
                result.append((room_name, False, status))
            else:
                newly_gone.add(room_name)
        elif past_cap or room_name in observer_only_rooms:
            result.append((room_name, True, status))
    return result, newly_gone


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
        review_floor: int,
        form_auto_retry_enabled: bool = False,
        recording: "RecordingConfig | None" = None,
        call_plans: CallPlanService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._livekit = livekit
        self._kms = kms
        self._audit = audit
        self._call_stream = call_stream
        self._interval_s = interval_s
        self._stuck_grace_s = stuck_grace_s
        self._max_call_duration_s = max_call_duration_s
        self._review_floor = review_floor
        self._form_auto_retry_enabled = form_auto_retry_enabled
        self._recording = recording
        self._call_plans = call_plans
        # Rooms observed GONE on the previous tick (per-process memory for the
        # two-tick confirmation; room names embed the tenant id). Replicas each
        # keep their own — close_call's row lock makes a double-close a no-op.
        self._gone_rooms_pending: set[str] = set()

    async def run(self) -> None:
        """Sweep immediately on boot, then every interval. Mirrors the worker-event
        consumer's resilience: any error logs and waits for the next tick."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Type name only — the sweep runs the transcript finalizer,
                # whose SQLAlchemy/Redis errors embed transcript text (PHI).
                logger.error(
                    "pipeline sweep failed (%s); retrying next interval", type(exc).__name__
                )
            await asyncio.sleep(self._interval_s)

    async def sweep_once(self) -> None:
        async with platform_session(self._sessionmaker) as session:
            tenant_ids = list((await session.execute(select(Tenant.id))).scalars().all())
        for tenant_id in tenant_ids:
            try:
                await self._sweep_tenant(tenant_id)
            except Exception as exc:  # one tenant's failure must not starve the rest
                # Type name only — same PHI-in-exception risk as the run loop.
                logger.error(
                    "sweep failed for tenant %s (%s); continuing", tenant_id, type(exc).__name__
                )

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
        # A gone room is only closed on its second consecutive sighting (see
        # rooms_to_close); first sightings wait in _gone_rooms_pending.
        closed = 0
        newly_gone: set[str] = set()
        if rows:
            candidate_rooms = [room_name_for_call(tenant_id, cid) for cid, _, _ in rows]
            live_rooms = await self._livekit.existing_rooms(candidate_rooms)
            observer_only: set[str] = set()
            for room_name in sorted(live_rooms):
                participants = await self._livekit.room_participants(room_name)
                if participants is None:
                    live_rooms.discard(room_name)  # vanished between the two probes
                elif all(is_observer_identity(p.identity) for p in participants):
                    # empty, or only supervisors/monitors — nothing can progress
                    observer_only.add(room_name)
            to_close, newly_gone = rooms_to_close(
                rows,
                live_rooms,
                observer_only,
                tenant_id,
                confirmed_gone=self._gone_rooms_pending,
            )
            for room_name, delete_first, status in to_close:
                if delete_first:
                    logger.warning(
                        "sweeper: room %s is dead (past cap or observer-only); deleting", room_name
                    )
                    await self._livekit.delete_room(room_name)
                closed_call = await close_call(
                    self._sessionmaker,
                    self._audit,
                    room_name,
                    status,
                    trigger="sweeper_reconcile",
                    actor_label="pipeline-sweeper",
                )
                if closed_call is not None:
                    ref, applied = closed_call
                    # Tell anyone tailing the live SSE before the finalizer
                    # deletes the stream (a swept call has no worker to publish).
                    await announce_terminal_status(self._call_stream, room_name, applied)
                    await finalize_transcript(self._sessionmaker, self._call_stream, ref, room_name)
                    if applied is CallStatus.CANCELED:
                        # A canceled close parks the form in AI_PROCESSING —
                        # resolve it now instead of leaving it to phase 3's
                        # grace-delayed sweep (the resolver never auto-requeues
                        # a canceled call).
                        await resolve_ai_processing(
                            self._sessionmaker,
                            self._audit,
                            ref,
                            trigger="sweeper_reconcile",
                            actor_label="pipeline-sweeper",
                        )
                    closed += 1
                    logger.info(
                        "sweeper: reconciled stuck call room %s as %s", room_name, applied.value
                    )

        # Roll the two-tick memory: this tenant's previous sightings are now
        # either closed, live again, or terminal (consumer won the race) — all
        # stale, so drop them. Keep other tenants' pending entries untouched and
        # carry over only this tick's first sightings.
        other_tenants_pending = {
            r
            for r in self._gone_rooms_pending
            if (room_ref := parse_room_name(r)) is None or room_ref.tenant_id != tenant_id
        }
        self._gone_rooms_pending = other_tenants_pending | newly_gone

        # Phase 3: resolve forms stranded in AI_PROCESSING (a crash between
        # closeout and post-call resolution leaks a concurrency slot forever —
        # the dispatcher counts AI_PROCESSING as active).
        resolved = await sweep_stuck_ai_processing(
            self._sessionmaker,
            self._audit,
            tenant_id,
            grace_s=self._stuck_grace_s,
        )

        # Phase 4: time-based dispatch wake-up — freed slots and/or queued forms.
        if closed or resolved or has_queued:
            await run_dispatch_pass(
                self._sessionmaker,
                tenant_id,
                self._livekit,
                self._kms,
                self._audit,
                recording=self._recording,
                plan_service=self._call_plans,
                retry_floor=self._review_floor,
            )
