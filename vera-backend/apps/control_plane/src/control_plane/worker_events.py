"""Consumes worker→control-plane events (Redis Streams consumer group) and
orchestrates the reaction. One consumer runs per control-plane process; the group
delivers each event to exactly one process, and entries a crashed process left
pending are reclaimed via XAUTOCLAIM (at-least-once). Handlers are idempotent, so
redelivery / a rare double-delivery is harmless.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from control_plane.livekit_gateway import LiveKitGateway
from vera_core.events import (
    WORKER_EVENTS_GROUP,
    WORKER_EVENTS_STREAM,
    CallFailedEvent,
    WorkerEvent,
    WorkerEventBus,
    parse_worker_event,
)
from vera_core.observability.correlation import parse_room_name

logger = logging.getLogger("control_plane.worker_events")

type EventHandler = Callable[[WorkerEvent], Awaitable[None]]

# The redis-py stubs type XREADGROUP/XAUTOCLAIM responses as broad unions (they
# also cover bytes-mode and other subcommands); with `decode_responses=True` and
# no `justid`, both always return this shape at runtime.
type _StreamEntries = list[tuple[str, dict[str, str]]]


class WorkerEventConsumer:
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
        self._redis = redis
        self._livekit = livekit
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._teardown_grace_ms = teardown_grace_ms
        self._consumer = consumer_name or f"{socket.gethostname()}:{os.getpid()}"
        self._bus = WorkerEventBus(redis)
        self._handlers: dict[str, EventHandler] = {"call.failed": self._handle_call_failed}

    async def run(self) -> None:
        """Ensure the group exists, then loop: reclaim stragglers, read new, dispatch.

        Group bootstrap lives inside the loop (guarded by `group_ready`) rather than
        before it, so a Redis blip at process startup is retried via the same
        back-off as steady-state errors instead of raising out of `run()` and
        killing the background task permanently.
        """
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
                logger.exception("worker-event consumer Redis error; backing off")
                await asyncio.sleep(1.0)

    async def _read_once(self) -> None:
        try:
            resp = await self._redis.xreadgroup(
                WORKER_EVENTS_GROUP,
                self._consumer,
                {WORKER_EVENTS_STREAM: ">"},
                count=16,
                block=self._block_ms,
            )
        except RedisTimeoutError:
            # redis-py turns an XREADGROUP BLOCK window with no new entries into a
            # raised TimeoutError (a per-command read deadline), not an empty result.
            # That is a normal idle tick — treat it as "no new events", NOT a Redis
            # error (which would log a traceback + back off). Mirrors RedisTranscriptStore.
            return
        if not resp:
            return
        streams = cast("list[tuple[str, _StreamEntries]]", resp)
        _stream, entries = streams[0]
        await self._dispatch(entries)

    async def _reclaim_stale(self) -> None:
        # Re-scans from the start of the stream (`start_id="0-0"`) on every call rather
        # than walking the returned cursor — fine given the low event volume here.
        result = await self._redis.xautoclaim(
            WORKER_EVENTS_STREAM,
            WORKER_EVENTS_GROUP,
            self._consumer,
            min_idle_time=self._reclaim_idle_ms,
            start_id="0-0",
            count=16,
        )
        _cursor, entries, _deleted = cast("tuple[str, _StreamEntries, list[str]]", result)
        await self._dispatch(entries)

    async def _dispatch(self, entries: _StreamEntries) -> None:
        """Process a batch of stream entries concurrently."""
        await asyncio.gather(*(self._process(entry_id, fields) for entry_id, fields in entries))

    async def _process(self, entry_id: str, fields: dict[str, str]) -> None:
        raw = fields.get("event")
        if raw is None:
            logger.warning("worker event %s has no payload; dropping", entry_id)
            await self._ack(entry_id)
            return
        try:
            event = parse_worker_event(raw)
        except Exception:
            logger.exception("dropping unparseable worker event %s", entry_id)
            await self._ack(entry_id)  # poison entry — drop so it can't wedge the group
            return
        handler = self._handlers.get(event.type)
        if handler is None:
            logger.warning("no handler for worker event type %s; dropping", event.type)
            await self._ack(entry_id)
            return
        try:
            await handler(event)
        except Exception:
            logger.exception("handler failed for event %s; leaving unacked for reclaim", entry_id)
            return  # do NOT ack → XAUTOCLAIM retries later (at-least-once)
        await self._ack(entry_id)

    async def _ack(self, entry_id: str) -> None:
        await self._redis.xack(WORKER_EVENTS_STREAM, WORKER_EVENTS_GROUP, entry_id)

    async def _handle_call_failed(self, event: WorkerEvent) -> None:
        assert isinstance(event, CallFailedEvent)
        if parse_room_name(event.room_name) is None:
            logger.warning("call.failed for non-vera room %s; ignoring", event.room_name)
            return
        await self._livekit.set_room_metadata(
            event.room_name, {"status": "call_failed", "reason": event.reason.value}
        )
        # Let the RoomMetadataChanged frame reach the browser before we tear the room down.
        if self._teardown_grace_ms:
            await asyncio.sleep(self._teardown_grace_ms / 1000)
        await self._livekit.delete_room(event.room_name)
