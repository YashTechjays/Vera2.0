"""Worker plan-load fail-safes: a bad/absent/corrupt plan must fall back to the static
agent (return None), never kill the call — and schema-drift must be distinguishable."""

from typing import Any

import fakeredis.aioredis
import pytest

from agent_worker import main as worker_main
from vera_core.call_plan import call_plan_key
from vera_core.config.settings import Settings
from vera_core.forms.planning import CallPlan, ContextItem, PlanTask
from vera_core.phi import PassthroughPHIBoundary

ROOM = "call--t--c"


def _settings() -> Settings:
    return Settings(_env_file=None)


async def _load(monkeypatch: pytest.MonkeyPatch, redis: Any) -> Any:
    monkeypatch.setattr(worker_main, "create_redis", lambda _url: redis)
    return await worker_main._load_plan_state(ROOM, "sid", PassthroughPHIBoundary(), _settings())


def _plan(tasks: list[PlanTask]) -> CallPlan:
    return CallPlan(call_id="c", room_name=ROOM, schema_version="2.1", tasks=tasks)


async def test_valid_plan_returns_state(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    plan = _plan([PlanTask(task_key="t", order=0, fields=[])])
    await redis.set(call_plan_key(ROOM), plan.model_dump_json(by_alias=True))
    state = await _load(monkeypatch, redis)
    assert state is not None and state.plan.schema_version == "2.1"


async def test_missing_plan_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await _load(monkeypatch, redis) is None


async def test_corrupt_plan_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Schema drift / corruption → ValidationError branch → static fallback (not a crash).
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.set(call_plan_key(ROOM), '{"unexpected": "shape"}')
    assert await _load(monkeypatch, redis) is None


async def test_empty_tasks_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # I4: a tasks=[] plan must not reach build_agent (would IndexError on tasks[0]).
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.set(call_plan_key(ROOM), _plan([]).model_dump_json(by_alias=True))
    assert await _load(monkeypatch, redis) is None


async def test_context_values_seeded_into_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Known context values (e.g. spouse_gender) must seed the answer map so gates/routing
    # can use them without asking.
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    plan = CallPlan(
        call_id="c",
        room_name=ROOM,
        schema_version="2.1",
        tasks=[PlanTask(task_key="t", order=0, fields=[])],
        context_knowledge=[
            ContextItem(field_path="sections.patient.spouse_gender", title="Spouse", value="Male"),
            ContextItem(field_path="sections.patient.dob", title="DOB", value=None),  # unfilled
        ],
    )
    await redis.set(call_plan_key(ROOM), plan.model_dump_json(by_alias=True))
    state = await _load(monkeypatch, redis)
    assert state is not None
    assert state.answers == {"sections.patient.spouse_gender": "Male"}  # None one excluded


async def test_redis_read_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomRedis:
        async def get(self, *_a: Any) -> str:
            raise RuntimeError("redis unreachable")

        async def aclose(self) -> None:
            pass

    assert await _load(monkeypatch, BoomRedis()) is None
