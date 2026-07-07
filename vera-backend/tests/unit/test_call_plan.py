"""The call-plan transport: a non-PHI CallPlan stored in / read from Redis by room name."""

import fakeredis.aioredis

from vera_core.call_plan import (
    InMemoryCallPlanStore,
    RedisCallPlanStore,
    call_plan_key,
)
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
