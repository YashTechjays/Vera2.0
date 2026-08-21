"""Post-call eval consumer: drains vera:post-call, re-reads each finished call's
transcript, and runs evaluate_call. Mirrors worker_events.WorkerEventConsumer for the
group/ack/reclaim + idle-TimeoutError discipline.

Jobs are enqueued by the worker-events close path (see worker_events._close_and_refill)
right after call_closeout parks the form in AI_PROCESSING. evaluate_call owns the
transition out of AI_PROCESSING; a form the eval finds already resolved (e.g. the
pipeline sweeper got there first) is skipped — redelivery is harmless."""

import asyncio
import logging
import os
import socket
from typing import Any, cast
from uuid import UUID

from opentelemetry import trace
from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.call_summary import snapshot_turns
from control_plane.dispatch import run_dispatch_pass
from vera_core.audit import AuditSink
from vera_core.call_stream import CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.events import (
    POST_CALL_GROUP,
    POST_CALL_STREAM,
    PostCallJob,
    PostCallJobBus,
    parse_post_call_job,
)
from vera_core.integrations.llm import LLMClient, TranscriptTurn
from vera_core.observability import TraceLinkStore, call_scoped_span, room_name_for_call
from vera_core.services.post_call_eval import EvalDeps, evaluate_call

logger = logging.getLogger("control_plane.post_call_consumer")

type _StreamEntries = list[tuple[str, dict[str, str]]]

_tracer = trace.get_tracer("vera.control_plane.post_call")

# A job that keeps failing (poison: bad schema shape, persistent DB violation)
# is dropped after this many deliveries instead of re-billing the LLM on every
# reclaim forever. The form stays in AI_PROCESSING; the pipeline sweeper's
# sweep_stuck_ai_processing resolves it to EXCEPTION_REVIEW after its grace.
MAX_DELIVERIES = 5


async def build_turns(
    call_stream: CallStreamService,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    call_id: UUID,
) -> list[TranscriptTurn]:
    # dev's snapshot_turns reads the live Redis stream while it exists, else the
    # persisted Transcript rows. Adapt its (source, role, text) turns to the
    # eval's own TranscriptTurn (seq is the extraction/evidence index).
    snap = await snapshot_turns(call_stream, sessionmaker, tenant_id, call_id)
    return [TranscriptTurn(seq=i, role=t.role, text=t.text) for i, t in enumerate(snap)]


class PostCallConsumer:
    def __init__(
        self,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        call_stream: CallStreamService,
        llm: LLMClient,
        audit: AuditSink,
        livekit: Any,
        *,
        kms: Any = None,
        recording: Any = None,
        plan_service: Any = None,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        review_floor: int = 60,
        auto_retry_enabled: bool = False,
        consumer_name: str | None = None,
        trace_links: TraceLinkStore | None = None,
    ) -> None:
        self._redis = redis
        self._sessionmaker = sessionmaker
        self._call_stream = call_stream
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._consumer = consumer_name or f"{socket.gethostname()}:{os.getpid()}"
        self._trace_links = trace_links
        self._bus = PostCallJobBus(redis)
        self._deps = EvalDeps(
            llm=llm,
            audit=audit,
            livekit=livekit,
            kms=kms,
            recording=recording,
            plan_service=plan_service,
            floor=review_floor,
            auto_retry_enabled=auto_retry_enabled,
        )

    async def run(self) -> None:
        group_ready = False
        while True:
            try:
                if not group_ready:
                    await self._bus.ensure_group()
                    group_ready = True
                await self._reclaim_stale()
                await self._read_once()
            except asyncio.CancelledError:
                raise
            except RedisError:
                logger.exception("post-call consumer Redis error; backing off")
                await asyncio.sleep(1.0)

    async def _read_once(self) -> None:
        try:
            resp = await self._redis.xreadgroup(
                POST_CALL_GROUP,
                self._consumer,
                {POST_CALL_STREAM: ">"},
                count=16,
                block=self._block_ms,
            )
        except RedisTimeoutError:
            return  # idle tick — see CLAUDE.md
        if not resp:
            return
        streams = cast("list[tuple[str, _StreamEntries]]", resp)
        _, entries = streams[0]
        await self._dispatch(entries)

    async def _reclaim_stale(self) -> None:
        result = await self._redis.xautoclaim(
            POST_CALL_STREAM,
            POST_CALL_GROUP,
            self._consumer,
            min_idle_time=self._reclaim_idle_ms,
            start_id="0-0",
            count=16,
        )
        # _cursor ignored: any stale entries beyond `count` drain on the next run() pass.
        _cursor, entries, _deleted = cast("tuple[str, _StreamEntries, list[str]]", result)
        await self._dispatch(entries)

    async def _dispatch(self, entries: _StreamEntries) -> None:
        await asyncio.gather(*(self._process(eid, f) for eid, f in entries))

    async def _process(self, entry_id: str, fields: dict[str, str]) -> None:
        raw = fields.get("job")
        if raw is None:
            await self._ack(entry_id)
            return
        try:
            job = parse_post_call_job(raw)
        except Exception:
            logger.exception("dropping unparseable post-call job %s", entry_id)
            await self._ack(entry_id)
            return
        try:
            await self._process_job(job)
        except Exception:
            if await self._deliveries(entry_id) >= MAX_DELIVERIES:
                logger.exception(
                    "post-call job %s failed %d times; dropping (form %s left in "
                    "AI_PROCESSING for the pipeline sweeper)",
                    entry_id,
                    MAX_DELIVERIES,
                    job.form_id,
                )
                await self._ack(entry_id)
                return
            logger.exception("post-call job %s failed; leaving unacked for reclaim", entry_id)
            return  # do NOT ack → XAUTOCLAIM retries (at-least-once)
        await self._ack(entry_id)

    async def _deliveries(self, entry_id: str) -> int:
        """Delivery count for one pending entry; 0 when it can't be determined
        (the safe direction — the entry stays unacked and retries)."""
        try:
            pending = await self._redis.xpending_range(
                POST_CALL_STREAM, POST_CALL_GROUP, min=entry_id, max=entry_id, count=1
            )
        except RedisError:
            return 0
        if not pending:
            return 0
        return int(pending[0].get("times_delivered", 0))

    async def _process_job(self, job: PostCallJob) -> None:
        room_name = room_name_for_call(job.tenant_id, job.call_id)
        # Joins the worker's trace for this call, so every eval generation below sums
        # into that call's total cost.
        async with call_scoped_span(
            _tracer, "vera.post_call.eval", room_name=room_name, trace_links=self._trace_links
        ):
            turns = await build_turns(
                self._call_stream, self._sessionmaker, job.tenant_id, job.call_id
            )
            async with tenant_session(self._sessionmaker, job.tenant_id) as session:
                outcome = await evaluate_call(
                    session,
                    self._deps,
                    tenant_id=job.tenant_id,
                    form_id=job.form_id,
                    call_id=job.call_id,
                    turns=turns,
                )
            logger.info(
                "post-call eval form=%s -> %s (%d answers, %d reviewed)",
                job.form_id,
                outcome.status.value,
                outcome.answers_written,
                len(outcome.reviewed_fields),
            )
        if not outcome.transitioned:
            return  # stale job: the form was left untouched, so no slot was freed
        # Outside the span above: that one joins the finished call's trace to attribute
        # its eval cost, and dispatching the freed slot is work for the NEXT call.
        await run_dispatch_pass(
            self._sessionmaker,
            job.tenant_id,
            self._deps.livekit,
            self._deps.kms,
            self._deps.audit,
            recording=self._deps.recording,
            plan_service=self._deps.plan_service,
            retry_floor=self._deps.floor,
        )

    async def _ack(self, entry_id: str) -> None:
        await self._redis.xack(POST_CALL_STREAM, POST_CALL_GROUP, entry_id)
