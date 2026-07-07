"""Call-plan transport: the compiled, PHI-free CallPlan for one call, keyed by room name.

The control plane compiles a plan at call start and `put`s it here; the agent worker
`get`s it when it joins the room (both derive the same room name — no shared state). The
plan carries no prefilled PHI (see `forms.planning`), so it is safe in Redis alongside the
tokens/reference-ids everything else caches — never raw values.
"""

from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.planning import CallPlan, compile_call_plan
from vera_core.models import PatientForm, SchemaVersion

CALL_PLAN_KEY_PREFIX = "vera:callplan:"


def call_plan_key(room_name: str) -> str:
    return f"{CALL_PLAN_KEY_PREFIX}{room_name}"


class CallPlanStore(Protocol):
    async def put(self, room_name: str, plan: CallPlan) -> None: ...

    async def get(self, room_name: str) -> CallPlan | None: ...


class RedisCallPlanStore:
    """Redis transport: one immutable JSON plan per room under a rolling backstop TTL
    so an abandoned plan self-clears."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def put(self, room_name: str, plan: CallPlan) -> None:
        await self._redis.set(
            call_plan_key(room_name), plan.model_dump_json(by_alias=True), ex=self._ttl_seconds
        )

    async def get(self, room_name: str) -> CallPlan | None:
        raw = await self._redis.get(call_plan_key(room_name))
        return CallPlan.model_validate_json(raw) if raw else None


class InMemoryCallPlanStore:
    """Process-local store for tests and single-process dev."""

    def __init__(self) -> None:
        self._plans: dict[str, CallPlan] = {}

    async def put(self, room_name: str, plan: CallPlan) -> None:
        self._plans[room_name] = plan

    async def get(self, room_name: str) -> CallPlan | None:
        return self._plans.get(room_name)


async def build_and_store_call_plan(
    session: AsyncSession,
    *,
    form: PatientForm,
    call_id: UUID,
    room_name: str,
    store: CallPlanStore,
) -> bool:
    """Compile the form's pinned schema into a plan and stash it for the worker. Returns
    False (no plan written) for a legacy v1 schema — the worker then falls back to the
    static agent. The current year comes from the DB clock (never the app clock)."""
    schema_json = (
        await session.execute(
            select(SchemaVersion.schema_json).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one_or_none()
    if schema_json is None or not is_v2(schema_json):
        return False
    year = (await session.execute(select(func.extract("year", func.now())))).scalar_one()
    plan = compile_call_plan(
        FormSchemaDoc.model_validate(schema_json),
        call_id=str(call_id),
        room_name=room_name,
        current_year=int(year),
    )
    await store.put(room_name, plan)
    return True
