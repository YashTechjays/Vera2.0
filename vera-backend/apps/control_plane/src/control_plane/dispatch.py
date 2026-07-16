"""Self-contained dispatch pass, run OUTSIDE any request transaction.

The dispatcher makes external calls (LiveKit room + SIP dial) and sleeps between
dials for carrier pacing — none of that may hold an HTTP request's transaction or
row locks. Hosts: the status endpoint's post-commit detached task and the
worker-event consumer (a call ended → a slot freed).
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from vera_core.db.rls import tenant_session
from vera_core.models import PatientForm
from vera_core.services.queue_dispatcher import try_dispatch

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vera_core.audit import AuditSink
    from vera_core.plan_store import CallPlanService
    from vera_core.services.recordings import RecordingConfig

logger = logging.getLogger(__name__)

# Detached dispatch tasks in flight. Strong refs (a bare create_task result can be
# GC'd mid-flight); tests drain this set to await post-commit dispatch work.
_PENDING: set[asyncio.Task[None]] = set()


def schedule_dispatch_pass(
    sessionmaker: "async_sessionmaker[AsyncSession]",
    tenant_id: "UUID",
    livekit: Any,
    kms: Any,
    audit: "AuditSink | None",
    *,
    wait_for_form_id: "UUID | None" = None,
    recording: "RecordingConfig | None" = None,
    plan_service: "CallPlanService | None" = None,
) -> None:
    """Fire-and-forget a dispatch pass on the running loop. See run_dispatch_pass
    for why this is a detached task and not fastapi.BackgroundTasks: background
    tasks run BEFORE yield-dependency teardown, i.e. before the request's
    transaction commits — the pass would see (and skip) the still-locked row."""
    _track(
        asyncio.create_task(
            _dispatch_pass(
                sessionmaker,
                tenant_id,
                livekit,
                kms,
                audit,
                wait_for_form_id=wait_for_form_id,
                recording=recording,
                plan_service=plan_service,
            )
        )
    )


def _track(task: asyncio.Task[None]) -> None:
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain_pending() -> None:
    """Await every in-flight detached dispatch task (test hook; also usable at
    shutdown). Exceptions are already swallowed inside _dispatch_pass."""
    while _PENDING:
        await asyncio.gather(*list(_PENDING), return_exceptions=True)


async def run_dispatch_pass(
    sessionmaker: "async_sessionmaker[AsyncSession]",
    tenant_id: "UUID",
    livekit: Any,
    kms: Any,
    audit: "AuditSink | None",
    *,
    wait_for_form_id: "UUID | None" = None,
    recording: "RecordingConfig | None" = None,
    plan_service: "CallPlanService | None" = None,
) -> None:
    """Await one dispatch pass, shielded from the caller's cancellation.

    The pass dials INSIDE its DB transaction, so cancelling it mid-pass (the
    consumer/sweeper tasks are cancelled on shutdown; a request handler can be
    cancelled on client disconnect) would roll back already-dialed Call rows
    while the SIP calls stay live — worker events would find no row and the
    next pass would redial the same payer. The pass therefore runs as a
    detached task in the shutdown-drained _PENDING set: the caller may be
    cancelled, but the pass itself always runs to completion and commits."""
    task = asyncio.create_task(
        _dispatch_pass(
            sessionmaker,
            tenant_id,
            livekit,
            kms,
            audit,
            wait_for_form_id=wait_for_form_id,
            recording=recording,
            plan_service=plan_service,
        )
    )
    _track(task)
    await asyncio.shield(task)


async def _dispatch_pass(
    sessionmaker: "async_sessionmaker[AsyncSession]",
    tenant_id: "UUID",
    livekit: Any,
    kms: Any,
    audit: "AuditSink | None",
    *,
    wait_for_form_id: "UUID | None" = None,
    recording: "RecordingConfig | None" = None,
    plan_service: "CallPlanService | None" = None,
) -> None:
    """One dispatch pass in a fresh tenant-scoped session; commits on success.
    Exception-safe: a failed pass logs and returns — queued forms are retried on
    the next triggering event."""
    try:
        if wait_for_form_id is not None:
            # Post-commit barrier: the scheduling request still holds the enqueued
            # row's FOR UPDATE lock until its transaction commits. A plain (non-
            # SKIP LOCKED) FOR UPDATE on that one row makes Postgres queue us
            # behind the commit — when it returns, the IN_QUEUE write is committed
            # and visible. The barrier transaction is closed before the pass runs.
            async with tenant_session(sessionmaker, tenant_id) as session:
                await session.execute(
                    select(PatientForm.id)
                    .where(PatientForm.id == wait_for_form_id)
                    .with_for_update()
                )
        async with tenant_session(sessionmaker, tenant_id) as session:
            await try_dispatch(
                session,
                tenant_id,
                livekit,
                kms,
                audit=audit,
                recording=recording,
                plan_service=plan_service,
            )
    except Exception as exc:
        # Type name only — SQLAlchemy statement errors embed the bound
        # parameters, and the pass touches patient_form rows (PHI).
        logger.error("dispatch pass failed for tenant %s (%s)", tenant_id, type(exc).__name__)
