"""Redis hand-off of the compiled call plan (control plane → agent worker).

One key per call room, TTL-bounded so an abandoned call self-clears:

- ``vera:callplan:{room}`` — the CallPlan JSON.

The plan carries raw prefilled values (PHI tokenization / sealing was removed as
a dev simplification), so this is synthetic-data-only until a protection
mechanism is reintroduced (see adr/devops-todo.md #8).
"""

from redis.asyncio import Redis

from vera_core.callplan.model import CallPlan

_PLAN_PREFIX = "vera:callplan:"


def call_plan_key(room_name: str) -> str:
    return f"{_PLAN_PREFIX}{room_name}"


class CallPlanStore:
    """Stash/fetch the compiled plan, keyed by room."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def put_plan(self, plan: CallPlan) -> None:
        await self._redis.set(call_plan_key(plan.room_name), plan.model_dump_json(), ex=self._ttl)

    async def get_plan(self, room_name: str) -> CallPlan | None:
        raw = await self._redis.get(call_plan_key(room_name))
        if raw is None:
            return None
        return CallPlan.model_validate_json(raw)

    async def delete(self, room_name: str) -> None:
        await self._redis.delete(call_plan_key(room_name))
