"""Shared Redis Streams consumer-group loop.

One consumer runs per control-plane process; the group delivers each entry to
exactly one process, and entries a crashed process left pending are reclaimed via
XAUTOCLAIM (at-least-once). Subclasses pin the stream/group/payload-field constants
and supply the parser + handler; handlers must be idempotent, so redelivery / a
rare double-delivery is harmless.

Ack discipline: a missing or unparseable payload is acked (a poison entry must not
wedge the group); a handler failure leaves the entry unacked so XAUTOCLAIM
redelivers it later.
"""

import asyncio
import logging
import os
import socket
from abc import ABC, abstractmethod
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

# The redis-py stubs type XREADGROUP/XAUTOCLAIM responses as broad unions (they
# also cover bytes-mode and other subcommands); with `decode_responses=True` and
# no `justid`, both always return this shape at runtime.
type StreamEntries = list[tuple[str, dict[str, str]]]


class StreamGroupConsumer[T](ABC):
    """Group bootstrap + reclaim/read/ack discipline over one (stream, group)."""

    # Pinned by each subclass (reuse the matching bus's constants).
    stream: str
    group: str
    payload_field: str

    def __init__(
        self,
        redis: Redis,
        *,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        consumer_name: str | None = None,
    ) -> None:
        self._redis = redis
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._consumer = consumer_name or f"{socket.gethostname()}:{os.getpid()}"
        self._log = logging.getLogger(type(self).__module__)

    @abstractmethod
    async def _ensure_group(self) -> None:
        """Create the consumer group if it doesn't exist (idempotent)."""

    @abstractmethod
    def _parse(self, raw: str) -> T:
        """Deserialize a payload; raising drops the entry as poison (acked)."""

    @abstractmethod
    async def _handle(self, entry_id: str, item: T) -> None:
        """Process one parsed entry; raising leaves it unacked for reclaim."""

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
                    await self._ensure_group()
                    group_ready = True
                await self._reclaim_stale()
                await self._read_once()
            except asyncio.CancelledError:
                raise
            except RedisError:
                self._log.exception("%s Redis error; backing off", type(self).__name__)
                await asyncio.sleep(1.0)

    async def _read_once(self) -> None:
        try:
            resp = await self._redis.xreadgroup(
                self.group,
                self._consumer,
                {self.stream: ">"},
                count=16,
                block=self._block_ms,
            )
        except RedisTimeoutError:
            # redis-py turns an XREADGROUP BLOCK window with no new entries into a
            # raised TimeoutError (a per-command read deadline), not an empty result.
            # That is a normal idle tick — treat it as "no new entries", NOT a Redis
            # error (which would log a traceback + back off). Mirrors RedisTranscriptStore.
            return
        if not resp:
            return
        streams = cast("list[tuple[str, StreamEntries]]", resp)
        _stream, entries = streams[0]
        await self._dispatch(entries)

    async def _reclaim_stale(self) -> None:
        # Re-scans from the start of the stream (`start_id="0-0"`) on every call rather
        # than walking the returned cursor — fine at these volumes; stale entries
        # beyond `count` drain on the next run() pass.
        result = await self._redis.xautoclaim(
            self.stream,
            self.group,
            self._consumer,
            min_idle_time=self._reclaim_idle_ms,
            start_id="0-0",
            count=16,
        )
        _cursor, entries, _deleted = cast("tuple[str, StreamEntries, list[str]]", result)
        await self._dispatch(entries)

    async def _dispatch(self, entries: StreamEntries) -> None:
        """Process a batch of stream entries concurrently."""
        await asyncio.gather(*(self._process(entry_id, fields) for entry_id, fields in entries))

    async def _process(self, entry_id: str, fields: dict[str, str]) -> None:
        raw = fields.get(self.payload_field)
        if raw is None:
            self._log.warning("stream entry %s has no payload; dropping", entry_id)
            await self._ack(entry_id)
            return
        try:
            item = self._parse(raw)
        except Exception:
            self._log.exception("dropping unparseable stream entry %s", entry_id)
            await self._ack(entry_id)  # poison entry — drop so it can't wedge the group
            return
        try:
            await self._handle(entry_id, item)
        except Exception:
            self._log.exception(
                "handler failed for entry %s; leaving unacked for reclaim", entry_id
            )
            return  # do NOT ack → XAUTOCLAIM retries later (at-least-once)
        await self._ack(entry_id)

    async def _ack(self, entry_id: str) -> None:
        await self._redis.xack(self.stream, self.group, entry_id)
