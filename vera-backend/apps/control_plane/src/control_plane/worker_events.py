"""Consumes worker→control-plane events (Redis Streams consumer group) and
orchestrates the reaction. The group/ack/reclaim loop lives in
`stream_consumer.StreamGroupConsumer`; this subclass supplies the event parsing
and the per-event-type handlers.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

from control_plane.livekit_gateway import LiveKitGateway
from control_plane.stream_consumer import StreamGroupConsumer
from vera_core.events import (
    CallFailedEvent,
    WorkerEvent,
    WorkerEventBus,
    parse_worker_event,
)
from vera_core.observability.correlation import parse_room_name

logger = logging.getLogger("control_plane.worker_events")

type EventHandler = Callable[[WorkerEvent], Awaitable[None]]


class WorkerEventConsumer(StreamGroupConsumer[WorkerEvent]):
    stream = WorkerEventBus.stream
    group = WorkerEventBus.group
    payload_field = WorkerEventBus.payload_field

    def __init__(
        self,
        redis: Redis,
        livekit: LiveKitGateway,
        *,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        teardown_grace_ms: int = 1_500,
        consumer_name: str | None = None,
    ) -> None:
        super().__init__(
            redis,
            block_ms=block_ms,
            reclaim_idle_ms=reclaim_idle_ms,
            consumer_name=consumer_name,
        )
        self._livekit = livekit
        self._teardown_grace_ms = teardown_grace_ms
        self._bus = WorkerEventBus(redis)
        self._handlers: dict[str, EventHandler] = {"call.failed": self._handle_call_failed}

    async def _ensure_group(self) -> None:
        await self._bus.ensure_group()

    def _parse(self, raw: str) -> WorkerEvent:
        return parse_worker_event(raw)

    async def _handle(self, entry_id: str, event: WorkerEvent) -> None:
        logger.info(
            "consumed worker event %s type=%s room=%s",
            entry_id,
            event.type,
            getattr(event, "room_name", "?"),
        )
        handler = self._handlers.get(event.type)
        if handler is None:
            logger.warning("no handler for worker event type %s; dropping", event.type)
            return
        await handler(event)

    async def _handle_call_failed(self, event: WorkerEvent) -> None:
        # Narrow without `assert` (stripped under `python -O`); handlers are keyed by
        # event.type, so this only guards against a future mis-registration.
        if not isinstance(event, CallFailedEvent):
            return
        if parse_room_name(event.room_name) is None:
            logger.warning("call.failed for non-vera room %s; ignoring", event.room_name)
            return
        logger.info(
            "call.failed room=%s reason=%s: setting metadata + deleting room",
            event.room_name,
            event.reason.value,
        )
        await self._livekit.set_room_metadata(
            event.room_name, {"status": "call_failed", "reason": event.reason.value}
        )
        # Let the RoomMetadataChanged frame reach the browser before we tear the room down.
        if self._teardown_grace_ms:
            await asyncio.sleep(self._teardown_grace_ms / 1000)
        await self._livekit.delete_room(event.room_name)
