"""Post-call eval consumer: drains vera:post-call, re-reads each finished call's
transcript, and runs evaluate_call. The group/ack/reclaim loop lives in
`stream_consumer.StreamGroupConsumer`; this subclass supplies the job parsing and
the evaluation itself.

Jobs are enqueued by the worker-events close path (see worker_events._close_and_refill)
right after call_closeout parks the form in AI_PROCESSING. evaluate_call owns the
transition out of AI_PROCESSING; a form the eval finds already resolved (e.g. the
pipeline sweeper got there first) is skipped — redelivery is harmless.
"""

import logging
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.stream_consumer import StreamGroupConsumer
from vera_core.audit import AuditSink
from vera_core.call_stream import TYPE_TRANSCRIPT, CallStreamService
from vera_core.db.rls import tenant_session
from vera_core.events import PostCallJob, PostCallJobBus, parse_post_call_job
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR
from vera_core.integrations.llm import LLMClient, TranscriptTurn
from vera_core.observability.correlation import room_name_for_call
from vera_core.services.post_call_eval import EvalDeps, evaluate_call

logger = logging.getLogger("control_plane.post_call_consumer")


async def build_turns(
    call_stream: CallStreamService, tenant_id: UUID, call_id: UUID
) -> list[TranscriptTurn]:
    room = room_name_for_call(tenant_id, call_id)
    events = await call_stream.read_all(room)
    return [
        TranscriptTurn(seq=i, role=e.data["role"], text=e.data["text"])
        for i, e in enumerate(e for e in events if e.type == TYPE_TRANSCRIPT)
    ]


class PostCallConsumer(StreamGroupConsumer[PostCallJob]):
    stream = PostCallJobBus.stream
    group = PostCallJobBus.group
    payload_field = PostCallJobBus.payload_field

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
        review_floor: int = REVIEW_CONFIDENCE_FLOOR,
        consumer_name: str | None = None,
    ) -> None:
        super().__init__(
            redis,
            block_ms=block_ms,
            reclaim_idle_ms=reclaim_idle_ms,
            consumer_name=consumer_name,
        )
        self._sessionmaker = sessionmaker
        self._call_stream = call_stream
        self._bus = PostCallJobBus(redis)
        self._deps = EvalDeps(
            llm=llm,
            audit=audit,
            livekit=livekit,
            kms=kms,
            recording=recording,
            plan_service=plan_service,
            floor=review_floor,
        )

    async def _ensure_group(self) -> None:
        await self._bus.ensure_group()

    def _parse(self, raw: str) -> PostCallJob:
        return parse_post_call_job(raw)

    async def _handle(self, entry_id: str, job: PostCallJob) -> None:
        turns = await build_turns(self._call_stream, job.tenant_id, job.call_id)
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
