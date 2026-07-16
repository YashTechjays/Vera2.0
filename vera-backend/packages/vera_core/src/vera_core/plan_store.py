"""Call Plan + Plan-Run state Redis transports (the worker's DB-free call context).

Two keys per call, both keyed by room name (the correlation key, like
`vera_core.transcript` / `vera_core.call_stream`):

* ``vera:call-plan:{room}`` — the compiled :class:`CallPlan`, one immutable JSON
  blob written once by the control plane at dispatch and read once by the agent
  worker at call start. Plain string key (SET + TTL), not a stream.
* ``vera:plan-run:{room}`` — the live run state, a Redis hash with two writers on
  DISJOINT fields: ``active_task_id`` (the conversational agent's cursor, its only
  write) and ``answer:{field_path}`` entries (the Observer, sole writer of
  answers). Per-field hash writes are atomic; every write slides the TTL.

Data posture: the fused CallPlan carries the form's raw intake values (prefill
hydration + the Known-information block — tokenization was dropped 2026-07-13),
and ``answer:*`` values hold what the live transcript holds — the same posture
as ``vera:transcript:*`` keys: values are never logged; parse failures are
counted, not printed.
"""

import json
import logging
from typing import Any, Protocol, cast

from redis.asyncio import Redis

from vera_core.forms.call_plan import CallPlan

logger = logging.getLogger(__name__)

_PLAN_KEY_PREFIX = "vera:call-plan:"
_RUN_KEY_PREFIX = "vera:plan-run:"

ACTIVE_TASK_FIELD = "active_task_id"
_ANSWER_FIELD_PREFIX = "answer:"


def call_plan_key(room_name: str) -> str:
    return f"{_PLAN_KEY_PREFIX}{room_name}"


def plan_run_key(room_name: str) -> str:
    return f"{_RUN_KEY_PREFIX}{room_name}"


class CallPlanStore(Protocol):
    async def put(self, room_name: str, plan_json: str) -> None: ...
    async def get(self, room_name: str) -> str | None: ...
    async def delete(self, room_name: str) -> None: ...


class RedisCallPlanStore:
    """One immutable blob per room: SET with TTL, GET, DEL."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def put(self, room_name: str, plan_json: str) -> None:
        await self._redis.set(call_plan_key(room_name), plan_json, ex=self._ttl_seconds)

    async def get(self, room_name: str) -> str | None:
        # create_redis sets decode_responses=True; redis-py types don't track it.
        return cast("str | None", await self._redis.get(call_plan_key(room_name)))

    async def delete(self, room_name: str) -> None:
        await self._redis.delete(call_plan_key(room_name))


class CallPlanService:
    """Produce/consume surface over a CallPlanStore — no caller touches raw Redis."""

    def __init__(self, store: CallPlanStore) -> None:
        self._store = store

    async def put(self, room_name: str, plan: CallPlan) -> None:
        await self._store.put(room_name, plan.model_dump_json(by_alias=True, exclude_none=True))

    async def get(self, room_name: str) -> CallPlan | None:
        """The stored plan, or None when missing or unparseable — the worker treats
        both as "no plan" and falls back to the legacy monolithic agent."""
        text = await self._store.get(room_name)
        if text is None:
            return None
        try:
            return CallPlan.model_validate_json(text)
        except Exception:  # plan blob content is config, but stay count/type-only anyway
            logger.warning("call plan %s: stored blob failed validation", room_name)
            return None

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)


class PlanRunStateStore(Protocol):
    async def set_field(self, room_name: str, field: str, value: str) -> None: ...
    async def get_field(self, room_name: str, field: str) -> str | None: ...
    async def get_all(self, room_name: str) -> dict[str, str]: ...
    async def delete(self, room_name: str) -> None: ...


class RedisPlanRunStateStore:
    """Hash transport; every write slides the backstop TTL (rolling, like the
    transcript stream) so state outlives any mid-call stall but not the day."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def set_field(self, room_name: str, field: str, value: str) -> None:
        key = plan_run_key(room_name)
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(key, field, value)
        pipe.expire(key, self._ttl_seconds)
        await pipe.execute()

    async def get_field(self, room_name: str, field: str) -> str | None:
        # create_redis sets decode_responses=True; redis-py types don't track it.
        return cast("str | None", await self._redis.hget(plan_run_key(room_name), field))

    async def get_all(self, room_name: str) -> dict[str, str]:
        return cast("dict[str, str]", await self._redis.hgetall(plan_run_key(room_name)))

    async def delete(self, room_name: str) -> None:
        await self._redis.delete(plan_run_key(room_name))


class PlanRunStateService:
    """The shared call-run state. Two writers, disjoint fields (see module doc)."""

    def __init__(self, store: PlanRunStateStore) -> None:
        self._store = store

    async def set_active_task(self, room_name: str, task_key: str) -> None:
        await self._store.set_field(room_name, ACTIVE_TASK_FIELD, task_key)

    async def get_active_task(self, room_name: str) -> str | None:
        return await self._store.get_field(room_name, ACTIVE_TASK_FIELD)

    async def record_answer(
        self,
        room_name: str,
        field_path: str,
        *,
        value: Any,
        ts: int,
        confidence: int | None = None,
        evidence_seq: int | None = None,
    ) -> None:
        payload = {
            "value": value,
            "confidence": confidence,
            "evidence_seq": evidence_seq,
            "ts": ts,
        }
        await self._store.set_field(
            room_name, f"{_ANSWER_FIELD_PREFIX}{field_path}", json.dumps(payload)
        )

    async def get_answers(self, room_name: str) -> dict[str, Any]:
        """{field_path: raw value} — the shape `conditions.evaluate` consumes.
        Malformed entries are skipped and counted, never printed (values are
        transcript-derived — treat as PHI)."""
        answers: dict[str, Any] = {}
        malformed = 0
        for field, raw in (await self._store.get_all(room_name)).items():
            if not field.startswith(_ANSWER_FIELD_PREFIX):
                continue
            try:
                answers[field[len(_ANSWER_FIELD_PREFIX) :]] = json.loads(raw)["value"]
            except Exception:
                malformed += 1
        if malformed:
            logger.warning(
                "plan run %s: skipped %d malformed answer entr%s",
                room_name,
                malformed,
                "y" if malformed == 1 else "ies",
            )
        return answers

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)
