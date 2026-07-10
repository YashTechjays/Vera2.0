"""Post-call eval consumer: drains vera:post-call, re-reads each finished call's
transcript, and runs evaluate_call. The group/ack/reclaim loop lives in
`stream_consumer.StreamGroupConsumer`; this subclass supplies the job parsing and
the evaluation itself.
"""

import logging
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.stream_consumer import StreamGroupConsumer
from vera_core.audit import AuditSink
from vera_core.db.rls import tenant_session
from vera_core.events import PostCallJob, PostCallJobBus, parse_post_call_job
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR
from vera_core.integrations.llm import LLMClient, TranscriptTurn
from vera_core.observability.correlation import room_name_for_call
from vera_core.services.post_call_eval import EvalDeps, evaluate_call
from vera_core.transcript import TranscriptService

logger = logging.getLogger("control_plane.post_call")


async def build_turns(
    transcript: TranscriptService, tenant_id: UUID, call_id: UUID
) -> list[TranscriptTurn]:
    room = room_name_for_call(tenant_id, call_id)
    events = await transcript.snapshot(room)
    return [TranscriptTurn(seq=i, role=e.role, text=e.text) for i, e in enumerate(events)]


class PostCallConsumer(StreamGroupConsumer[PostCallJob]):
    stream = PostCallJobBus.stream
    group = PostCallJobBus.group
    payload_field = PostCallJobBus.payload_field

    def __init__(
        self,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        transcript: TranscriptService,
        llm: LLMClient,
        audit: AuditSink,
        livekit: Any,
        *,
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
        self._transcript = transcript
        self._bus = PostCallJobBus(redis)
        self._deps = EvalDeps(llm=llm, audit=audit, livekit=livekit, floor=review_floor)

    async def _ensure_group(self) -> None:
        await self._bus.ensure_group()

    def _parse(self, raw: str) -> PostCallJob:
        return parse_post_call_job(raw)

    async def _handle(self, entry_id: str, job: PostCallJob) -> None:
        turns = await build_turns(self._transcript, job.tenant_id, job.call_id)
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
