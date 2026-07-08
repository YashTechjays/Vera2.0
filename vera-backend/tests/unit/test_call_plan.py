"""The call-plan transport: a non-PHI CallPlan stored in / read from Redis by room name."""

from typing import Any
from unittest.mock import MagicMock

import fakeredis.aioredis

from vera_core.call_plan import (
    InMemoryCallPlanStore,
    RedisCallPlanStore,
    _compile_and_store,
    call_plan_key,
)
from vera_core.db import uuid7
from vera_core.forms.planning import CallPlan, PlanTask


def _plan() -> CallPlan:
    return CallPlan(
        call_id="c1",
        room_name="room--t--c1",
        schema_version="2.1",
        tasks=[PlanTask(task_key="main", order=0, fields=[])],
    )


async def test_redis_store_round_trips_the_plan() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisCallPlanStore(redis, ttl_seconds=3600)
    plan = _plan()

    await store.put(plan.room_name, plan)

    assert await store.get(plan.room_name) == plan


async def test_redis_store_sets_ttl_under_the_room_key() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisCallPlanStore(redis, ttl_seconds=1800)
    plan = _plan()

    await store.put(plan.room_name, plan)

    ttl = await redis.ttl(call_plan_key(plan.room_name))
    assert 0 < ttl <= 1800


async def test_get_missing_room_returns_none() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisCallPlanStore(redis, ttl_seconds=3600)
    assert await store.get("no-such-room") is None


async def test_in_memory_store_round_trips() -> None:
    store = InMemoryCallPlanStore()
    plan = _plan()
    await store.put(plan.room_name, plan)
    assert await store.get(plan.room_name) == plan
    assert await store.get("missing") is None


async def _compile(session: Any, schema_json: Any) -> tuple[bool, InMemoryCallPlanStore]:
    store = InMemoryCallPlanStore()
    written = await _compile_and_store(
        session, schema_json, call_id=uuid7(), room_name="r", store=store
    )
    return written, store


async def test_missing_schema_json_is_false_and_writes_nothing() -> None:
    # I7: a v2 form whose SchemaVersion has no schema_json → benign-looking but a data bug.
    written, store = await _compile(MagicMock(), None)
    assert written is False
    assert await store.get("r") is None


async def test_legacy_v1_schema_is_false() -> None:
    written, _ = await _compile(MagicMock(), {"dsl_version": "1.0", "sections": {}})
    assert written is False


async def test_malformed_v2_schema_is_false_not_raising() -> None:
    # I6: a v2-tagged but invalid schema must fall back (return False), never raise — so
    # start_call doesn't 500 and try_dispatch doesn't bounce the form. MagicMock session is
    # never reached (model_validate fails first).
    written, store = await _compile(MagicMock(), {"dsl_version": "2.1"})
    assert written is False
    assert await store.get("r") is None
