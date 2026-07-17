"""Per-tenant notification stream: publish shape, tail-from-now anchor, idle
keepalive ticks (redis.asyncio BLOCK reads RAISE TimeoutError — repo footgun)."""

import json
from typing import Any
from uuid import uuid4

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from vera_core.notifications import (
    TYPE_INTERVENTION_NEEDED,
    Notification,
    NotificationAudience,
    NotificationService,
    RedisNotificationStore,
    notify_stream_key,
)


class _FakePipe:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def xadd(self, *args: Any, **kwargs: Any) -> None:
        self._ops.append(("xadd", args, kwargs))

    def expire(self, *args: Any, **kwargs: Any) -> None:
        self._ops.append(("expire", args, kwargs))

    async def execute(self) -> None:
        self._redis.ops.extend(self._ops)


class _FakeRedis:
    def __init__(self) -> None:
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.xrevrange_result: list[tuple[str, dict[str, str]]] = []
        # Each item: an exception to raise, or an xread response to return.
        self.xread_script: list[Any] = []

    def pipeline(self, transaction: bool = False) -> _FakePipe:
        return _FakePipe(self)

    async def xrevrange(self, key: str, max: str, min: str, count: int) -> Any:
        return self.xrevrange_result

    async def xread(self, streams: dict[str, str], block: int) -> Any:
        self.ops.append(("xread", (dict(streams),), {"block": block}))
        item = self.xread_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _notification() -> Notification:
    return Notification(
        type=TYPE_INTERVENTION_NEEDED,
        audience=NotificationAudience(kind="tenant"),
        data={"call_id": "c1", "score": 30, "flag": "conversation_loop", "reason": "r"},
        ts=123,
    )


@pytest.mark.asyncio
async def test_publish_xadds_with_maxlen_and_ttl() -> None:
    redis = _FakeRedis()
    tenant_id = uuid4()
    service = NotificationService(RedisNotificationStore(redis))  # type: ignore[arg-type]
    await service.publish(tenant_id, _notification())
    kinds = [op[0] for op in redis.ops]
    assert kinds == ["xadd", "expire"]
    _, xadd_args, xadd_kwargs = redis.ops[0]
    assert xadd_args[0] == notify_stream_key(tenant_id)
    assert json.loads(xadd_args[1]["n"])["type"] == TYPE_INTERVENTION_NEEDED
    assert xadd_kwargs == {"maxlen": 1000, "approximate": True}


@pytest.mark.asyncio
async def test_tail_anchors_past_existing_entries_and_ticks_on_idle() -> None:
    redis = _FakeRedis()
    tenant_id = uuid4()
    key = notify_stream_key(tenant_id)
    redis.xrevrange_result = [("5-1", {"n": _notification().model_dump_json()})]
    redis.xread_script = [
        RedisTimeoutError(),  # idle BLOCK window -> keepalive tick
        [(key, [("6-1", {"n": _notification().model_dump_json()})])],
    ]
    service = NotificationService(RedisNotificationStore(redis))  # type: ignore[arg-type]
    it = service.tail(tenant_id)
    assert await anext(it) is None  # the idle tick
    item = await anext(it)
    assert item is not None
    entry_id, n = item
    assert entry_id == "6-1"
    assert n.audience.kind == "tenant"
    # Anchor: the first xread must start AFTER the pre-existing entry, not at 0/"$".
    first_xread = next(op for op in redis.ops if op[0] == "xread")
    assert first_xread[1][0] == {key: "5-1"}


@pytest.mark.asyncio
async def test_tail_skips_malformed_entries() -> None:
    redis = _FakeRedis()
    tenant_id = uuid4()
    key = notify_stream_key(tenant_id)
    redis.xread_script = [
        [(key, [("1-1", {"n": "not json"}), ("1-2", {"n": _notification().model_dump_json()})])],
    ]
    service = NotificationService(RedisNotificationStore(redis))  # type: ignore[arg-type]
    item = await anext(service.tail(tenant_id))
    assert item is not None and item[0] == "1-2"
