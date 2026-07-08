"""Call-plan transport: the compiled, PHI-free CallPlan for one call, keyed by room name.

The control plane compiles a plan at call start and `put`s it here; the agent worker
`get`s it when it joins the room (both derive the same room name — no shared state). The
plan carries no prefilled PHI (see `forms.planning`), so it is safe in Redis alongside the
tokens/reference-ids everything else caches — never raw values.
"""

import logging
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.intake import iter_leaf_answers
from vera_core.forms.planning import CallPlan, compile_call_plan
from vera_core.models import FormSchema, PatientForm, SchemaVersion
from vera_core.models.enums import VersionStatus

logger = logging.getLogger(__name__)

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


async def _compile_and_store(
    session: AsyncSession,
    schema_json: dict[str, Any] | None,
    *,
    call_id: UUID,
    room_name: str,
    store: CallPlanStore,
    prefill: Mapping[str, str] = {},
) -> bool:
    """Compile a schema document into a plan and stash it for the worker. Returns False (no
    plan written; the worker falls back to the static agent) for a missing/legacy/malformed
    schema — a bad schema never 500s call-start nor bounces the form, matching the worker's
    fail-safe. The current year comes from the DB clock (never the app clock)."""
    if schema_json is None:
        # A v2 form whose pinned SchemaVersion carries no schema_json is a data bug, not the
        # benign v1 case below — surface it distinctly.
        logger.warning("call plan: no schema_json for call %s; static fallback (data bug)", call_id)
        return False
    if not is_v2(schema_json):
        return False  # legacy v1 form — benign; the worker runs the static agent
    try:
        doc = FormSchemaDoc.model_validate(schema_json)
    except ValidationError:
        logger.exception(
            "call plan: v2 schema failed to parse for call %s; static fallback", call_id
        )
        return False
    year = (await session.execute(select(func.extract("year", func.now())))).scalar_one()
    plan = compile_call_plan(
        doc, call_id=str(call_id), room_name=room_name, current_year=int(year), prefill=prefill
    )
    await store.put(room_name, plan)
    return True


def _prefill_from_form(form: PatientForm) -> dict[str, str]:
    """The patient's known values as {field_path: value}. `intake_payload` is section-nested
    (no `sections.` wrapper), so wrap it before flattening to get root-anchored paths that
    match `PlanField.field_path`."""
    payload = {"sections": form.intake_payload} if form.intake_payload else {}
    return {path: str(value) for path, value in iter_leaf_answers(payload)}


async def build_and_store_call_plan(
    session: AsyncSession,
    *,
    form: PatientForm,
    call_id: UUID,
    room_name: str,
    store: CallPlanStore,
) -> bool:
    """Compile the form's pinned schema version into a plan (prefilled from the form's
    intake values) and stash it for the worker."""
    schema_json = (
        await session.execute(
            select(SchemaVersion.schema_json).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one_or_none()
    return await _compile_and_store(
        session,
        schema_json,
        call_id=call_id,
        room_name=room_name,
        store=store,
        prefill=_prefill_from_form(form),
    )


async def build_and_store_call_plan_for_type(
    session: AsyncSession,
    *,
    insurance_type: str,
    call_id: UUID,
    room_name: str,
    store: CallPlanStore,
) -> bool:
    """Compile the currently-published schema for an insurance type (no form needed) — the
    Voice Lab path, so a QA session drives the plan agent instead of the static fallback."""
    schema_json = (
        await session.execute(
            select(SchemaVersion.schema_json)
            .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
            .where(
                FormSchema.insurance_type == insurance_type,
                SchemaVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
    return await _compile_and_store(
        session, schema_json, call_id=call_id, room_name=room_name, store=store
    )
