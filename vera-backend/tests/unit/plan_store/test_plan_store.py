"""CallPlan blob store + PlanRunState hash semantics (service over the Redis
impls, backed by fakeredis — real SET/HSET/EXPIRE/pipeline semantics)."""

import json
import uuid
from typing import cast

import fakeredis
import pytest
from redis.asyncio import Redis

from vera_core.forms.call_plan import CallPlan, PlanSession, PlanTask
from vera_core.plan_store import (
    ACTIVE_TASK_FIELD,
    CallPlanService,
    PlanRunStateService,
    RedisCallPlanStore,
    RedisPlanRunStateStore,
    call_plan_key,
    plan_run_key,
)

PLAN = CallPlan(
    schema_name="Infertility",
    insurance_type="ibv_standard",
    dsl_version="2.1",
    schema_version_id=uuid.uuid4(),
    session=PlanSession(persona="p", goal="g", base_instructions="b"),
    tasks=[PlanTask(task_key="t1", title="T1", prompt="ask things")],
)

ROOM = "call--t--c"


def _redis() -> Redis:
    return cast(Redis, fakeredis.aioredis.FakeRedis(decode_responses=True))


def _plan_svc(redis: Redis) -> CallPlanService:
    return CallPlanService(RedisCallPlanStore(redis, ttl_seconds=100))


def _run_svc(redis: Redis) -> PlanRunStateService:
    return PlanRunStateService(RedisPlanRunStateStore(redis, ttl_seconds=100))


def test_key_prefixes() -> None:
    assert call_plan_key(ROOM) == f"vera:call-plan:{ROOM}"
    assert plan_run_key(ROOM) == f"vera:plan-run:{ROOM}"


class TestCallPlanService:
    @pytest.mark.asyncio
    async def test_put_get_round_trip(self) -> None:
        svc = _plan_svc(_redis())
        await svc.put(ROOM, PLAN)
        assert await svc.get(ROOM) == PLAN

    @pytest.mark.asyncio
    async def test_put_sets_ttl(self) -> None:
        redis = _redis()
        await _plan_svc(redis).put(ROOM, PLAN)
        assert await redis.ttl(call_plan_key(ROOM)) == 100

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self) -> None:
        assert await _plan_svc(_redis()).get(ROOM) is None

    @pytest.mark.asyncio
    async def test_get_corrupt_returns_none_never_raises(self) -> None:
        redis = _redis()
        await redis.set(call_plan_key(ROOM), "{not json")
        assert await _plan_svc(redis).get(ROOM) is None

    @pytest.mark.asyncio
    async def test_clear_deletes_key(self) -> None:
        svc = _plan_svc(_redis())
        await svc.put(ROOM, PLAN)
        await svc.clear(ROOM)
        assert await svc.get(ROOM) is None


class TestPlanRunStateService:
    @pytest.mark.asyncio
    async def test_active_task_cursor_round_trip(self) -> None:
        svc = _run_svc(_redis())
        assert await svc.get_active_task(ROOM) is None
        await svc.set_active_task(ROOM, "insurance_basics")
        assert await svc.get_active_task(ROOM) == "insurance_basics"

    @pytest.mark.asyncio
    async def test_record_answer_and_get_answers_unwrapped(self) -> None:
        svc = _run_svc(_redis())
        await svc.record_answer(
            ROOM, "sections.a.b", value="Yes", confidence=90, evidence_seq=4, ts=1
        )
        await svc.record_answer(ROOM, "sections.a.c", value="No", ts=2)
        assert await svc.get_answers(ROOM) == {"sections.a.b": "Yes", "sections.a.c": "No"}

    @pytest.mark.asyncio
    async def test_answers_and_cursor_are_disjoint_fields(self) -> None:
        redis = _redis()
        svc = _run_svc(redis)
        await svc.set_active_task(ROOM, "t1")
        await svc.record_answer(ROOM, "sections.a.b", value="x", ts=1)
        fields = await redis.hgetall(plan_run_key(ROOM))
        assert ACTIVE_TASK_FIELD in fields
        assert "answer:sections.a.b" in fields
        # the cursor never shows up as an answer
        assert await svc.get_answers(ROOM) == {"sections.a.b": "x"}

    @pytest.mark.asyncio
    async def test_malformed_answer_field_skipped(self) -> None:
        redis = _redis()
        svc = _run_svc(redis)
        await svc.record_answer(ROOM, "sections.a.b", value="x", ts=1)
        await redis.hset(plan_run_key(ROOM), "answer:sections.a.broken", "{nope")
        assert await svc.get_answers(ROOM) == {"sections.a.b": "x"}

    @pytest.mark.asyncio
    async def test_writes_slide_ttl(self) -> None:
        redis = _redis()
        await _run_svc(redis).set_active_task(ROOM, "t1")
        assert await redis.ttl(plan_run_key(ROOM)) == 100

    @pytest.mark.asyncio
    async def test_answer_payload_shape(self) -> None:
        redis = _redis()
        await _run_svc(redis).record_answer(
            ROOM, "sections.a.b", value="Yes", confidence=88, evidence_seq=7, ts=123
        )
        raw = await redis.hget(plan_run_key(ROOM), "answer:sections.a.b")
        assert raw is not None
        assert json.loads(raw) == {
            "value": "Yes",
            "confidence": 88,
            "evidence_seq": 7,
            "ts": 123,
        }

    @pytest.mark.asyncio
    async def test_clear_deletes_hash(self) -> None:
        svc = _run_svc(_redis())
        await svc.set_active_task(ROOM, "t1")
        await svc.clear(ROOM)
        assert await svc.get_active_task(ROOM) is None
