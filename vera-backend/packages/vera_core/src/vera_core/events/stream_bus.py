"""Shared publish side of a Redis Stream + its consumer-group bootstrap.

Each concrete bus pins one (stream, group, payload-field) triple and adds a typed
``emit``; the XADD/XGROUP mechanics live here once.
"""

from redis.asyncio import Redis
from redis.exceptions import ResponseError


class StreamBus:
    """XADD publish + consumer-group bootstrap for one (stream, group) pair."""

    # Pinned by each subclass.
    stream: str
    group: str
    payload_field: str

    def __init__(self, redis: Redis, *, maxlen: int = 10_000) -> None:
        self._redis = redis
        self._maxlen = maxlen

    async def _emit_raw(self, payload: str) -> None:
        await self._redis.xadd(
            self.stream,
            {self.payload_field: payload},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def ensure_group(self) -> None:
        try:
            # id="0" (not "$"): the group starts at the beginning of the stream, so
            # events published before the group first exists are still delivered
            # (at-least-once across bootstrap) instead of being silently dropped.
            await self._redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
