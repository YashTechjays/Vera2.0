"""User-scoped realtime notifications over a per-tenant Redis stream.

The control plane publishes (the worker-event consumer's intervention alerts
today; anything user-facing later) and GET /notifications/stream tails.
Delivery is fan-out-with-filtering: every notification carries an `audience`
(one user, or the whole tenant) and each authenticated SSE connection forwards
only what is addressed to its user. Ephemeral by design: no DB persistence,
MAXLEN-trimmed, rolling TTL — a reconnecting client starts from "now" and
recovers current state from the REST API (the stream is an accelerant, never
the source of truth).

PHI: the health observer's intervention-needed notifications carry only
`call_id`/`score`/`flag` (minimum-necessary, 2026-07-18) — the LLM's `reason`
sentence is deliberately left out of `data` and stays in `CallEvent.detail`
instead. Content is never logged here (type names only).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = logging.getLogger(__name__)

_KEY_PREFIX = "vera:notify:"
_FIELD = "n"

TYPE_INTERVENTION_NEEDED = "intervention_needed"


def notify_stream_key(tenant_id: UUID) -> str:
    return f"{_KEY_PREFIX}{tenant_id}"


class NotificationAudience(BaseModel):
    """Who a notification is addressed to. kind="user" requires user_id."""

    kind: Literal["user", "tenant"]
    user_id: str | None = None


class Notification(BaseModel):
    type: str  # "intervention_needed" | future types
    audience: NotificationAudience
    data: dict[str, Any]
    ts: int  # epoch milliseconds


class RedisNotificationStore:
    """Redis Streams transport. MAXLEN bounds per-tenant memory; the rolling TTL
    self-clears idle tenants' streams."""

    def __init__(
        self, redis: Redis, *, maxlen: int = 1000, ttl_seconds: int = 86_400, block_ms: int = 5000
    ) -> None:
        self._redis = redis
        self._maxlen = maxlen
        self._ttl_seconds = ttl_seconds
        self._block_ms = block_ms

    async def publish(self, tenant_id: UUID, notification: Notification) -> None:
        key = notify_stream_key(tenant_id)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(
            key, {_FIELD: notification.model_dump_json()}, maxlen=self._maxlen, approximate=True
        )
        pipe.expire(key, self._ttl_seconds)
        await pipe.execute()

    async def tail(self, tenant_id: UUID) -> AsyncIterator[tuple[str, Notification] | None]:
        """Tail from "now". Yields None on every idle BLOCK window so the SSE
        endpoint can emit a keepalive (same contract as RedisCallStreamStore.read).

        The anchor id is resolved ONCE via XREVRANGE and advanced per entry —
        re-issuing XREAD with "$" on every tick would silently drop entries
        published between two ticks."""
        key = notify_stream_key(tenant_id)
        newest = await self._redis.xrevrange(key, "+", "-", count=1)
        anchored = cast("list[tuple[str, dict[str, str]]]", newest or [])
        last_id = anchored[0][0] if anchored else "0-0"
        while True:
            try:
                result = await self._redis.xread({key: last_id}, block=self._block_ms)
            except RedisTimeoutError:
                # BLOCK with no entries RAISES (per-command read deadline) — idle tick.
                result = None
            if not result:
                yield None
                continue
            entries = cast("list[tuple[str, list[tuple[str, dict[str, str]]]]]", result)[0][1]
            for entry_id, fields in entries:
                last_id = entry_id
                raw = fields.get(_FIELD)
                if raw is None:
                    continue
                try:
                    yield entry_id, Notification.model_validate_json(raw)
                except Exception as exc:  # content may be PHI — type name only
                    logger.warning("skipping malformed notification (%s)", type(exc).__name__)


class NotificationService:
    """Produce/consume surface — no caller touches raw Redis (mirrors
    CallStreamService)."""

    def __init__(self, store: RedisNotificationStore) -> None:
        self._store = store

    async def publish(self, tenant_id: UUID, notification: Notification) -> None:
        await self._store.publish(tenant_id, notification)

    def tail(self, tenant_id: UUID) -> AsyncIterator[tuple[str, Notification] | None]:
        return self._store.tail(tenant_id)
