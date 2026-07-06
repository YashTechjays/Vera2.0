"""Integration tests for the /calls endpoints.

All three endpoints are tested against a live RLS-enforcing Postgres
connection with a FakeLiveKit injected in the authz_app fixture (see conftest.py).
"""

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.callplan import CallPlan, call_plan_key
from vera_core.config import Settings
from vera_core.db import uuid7
from vera_core.models import Call, PatientForm
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType
from vera_core.observability.correlation import parse_room_name
from vera_core.redis import create_redis


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# seeded_form_id — a PatientForm row owned by rbac_world.tenant_id.
# PatientForm requires a schema_version_id (FK RESTRICT) so we need a
# FormSchema + SchemaVersion first, created with a superuser (BYPASSRLS) session.
# We build a fresh sessionmaker from `database_url` directly.
#
# `form_schema.insurance_type` is a globally UNIQUE, CHECK-constrained catalog key
# (only INFERTILITY_TREATMENT is valid), so the schema row is find-or-create: CI
# runs `scripts/seed.py` before pytest and already publishes that schema, and a
# raw insert would collide on uq_form_schema_insurance_type. Teardown removes only
# the rows this fixture actually created — the shared schema is left intact for the
# seed / other tests / `tests/integration/db/test_seed_form_schemas.py`.
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_form_id(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """Ensure an INFERTILITY_TREATMENT FormSchema → SchemaVersion chain exists
    (reusing the seeded one if present), attach a PatientForm to it, and yield the
    PatientForm.id. Cleans up on teardown so rbac_world can delete the tenant
    without a FK violation."""
    patient_form_id = uuid7()

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session, session.begin():
            schema = (
                await session.execute(
                    select(FormSchema).where(
                        FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                    )
                )
            ).scalar_one_or_none()
            created_schema = schema is None
            if schema is None:
                schema = FormSchema(
                    id=uuid7(),
                    insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                    name="Test Schema",
                )
                session.add(schema)
                await session.flush()
                schema_version = SchemaVersion(
                    id=uuid7(), schema_id=schema.id, version=1, schema_json={}
                )
                session.add(schema_version)
                await session.flush()
                schema_version_id = schema_version.id
            else:
                schema_version_id = (
                    await session.execute(
                        select(SchemaVersion.id)
                        .where(SchemaVersion.schema_id == schema.id)
                        .order_by(SchemaVersion.version.desc())
                        .limit(1)
                    )
                ).scalar_one()
            form_schema_id = schema.id
            session.add(
                PatientForm(
                    id=patient_form_id,
                    tenant_id=rbac_world.tenant_id,
                    schema_version_id=schema_version_id,
                    patient_name="Test Patient",
                )
            )

        yield patient_form_id

        # Teardown: remove all call rows referencing this form (and their events)
        # before rbac_world deletes the tenant. Only drop the schema chain if this
        # fixture created it — a seeded/shared schema must survive.
        async with sessionmaker() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM call_event WHERE call_id IN "
                    "(SELECT id FROM call WHERE form_id = :fid)"
                ).bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM call WHERE form_id = :fid").bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE id = :fid").bindparams(fid=patient_form_id)
            )
            if created_schema:
                await session.execute(
                    text("DELETE FROM schema_version WHERE id = :sid").bindparams(
                        sid=schema_version_id
                    )
                )
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=form_schema_id)
                )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_calls_empty_initially(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    """Before any call is created the active list is empty for this tenant."""
    resp = await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


@pytest.mark.asyncio
async def test_list_calls_empty_then_populated(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    # create a call
    resp = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()["data"]
    assert summary["status"] == "initiated"
    assert parse_room_name(summary["room_name"]) is not None

    # it now appears in the list
    lst = await client.get(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
    )
    assert lst.status_code == 200, lst.text
    assert any(c["id"] == summary["id"] for c in lst.json()["data"])


@pytest.mark.asyncio
async def test_join_token_returns_room_scoped_token(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]
    room = created.json()["data"]["room_name"]

    tok = await client.get(
        f"/api/v1/calls/{call_id}/join-token",
        headers=_auth(rbac_world.admin_token),
    )
    assert tok.status_code == 200, tok.text
    body = tok.json()["data"]
    assert body["room_name"] == room
    assert body["token"].startswith("faketoken:")


@pytest.mark.asyncio
async def test_create_call_unknown_form_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(uuid4())},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_join_token_unknown_call_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.get(
        f"/api/v1/calls/{uuid4()}/join-token",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_calls_require_auth(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    """All three endpoints deny unauthenticated callers."""
    resp_list = await client.get("/api/v1/calls")
    assert resp_list.status_code == 401

    resp_create = await client.post(
        "/api/v1/calls",
        json={"form_id": str(seeded_form_id)},
    )
    assert resp_create.status_code == 401

    resp_token = await client.get(f"/api/v1/calls/{uuid4()}/join-token")
    assert resp_token.status_code == 401


# ---------------------------------------------------------------------------
# Call-plan stash (M1 section-wise runtime): POST /calls compiles the form's
# schema into a CallPlan in Redis, keyed by room name.
# ---------------------------------------------------------------------------

# Minimal compilable schema DSL (the real one is exercised by unit tests).
_MINI_SCHEMA: dict[str, Any] = {
    "constraint_library": {},
    "global_policies": [
        {"title": "Persona", "source": "persona.py:AGENT_PERSONA", "exact_text": "test persona"}
    ],
    "phase_order": {"phase_1": ["AGENT_PERSONA", "<SECTIONS>"]},
    "sections": [
        {
            "section_key": "patient_information",
            "title": "Patient Information",
            "section_role": "context",
            "properties": {
                "patient_name": {"type": "string", "title": "Patient Name", "confirm_only": True}
            },
        },
        {
            "section_key": "s1",
            "title": "Section One",
            "phase_key": "phase_1",
            "properties": {"f1": {"type": "string", "title": "Field One"}},
        },
    ],
}


@pytest.fixture
async def compilable_form_id(
    database_url: str,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,  # guarantees the FormSchema family exists
) -> AsyncGenerator[UUID]:
    """A PatientForm bound to a NEW SchemaVersion carrying a compilable mini
    schema, with an intake payload that produces a PHI prefill."""
    patient_form_id = uuid7()
    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session, session.begin():
            schema_id = (
                await session.execute(
                    select(FormSchema.id).where(
                        FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value
                    )
                )
            ).scalar_one()
            next_version = (
                await session.execute(
                    select(func.max(SchemaVersion.version)).where(
                        SchemaVersion.schema_id == schema_id
                    )
                )
            ).scalar() or 0
            schema_version = SchemaVersion(
                id=uuid7(),
                schema_id=schema_id,
                version=next_version + 1,
                schema_json=_MINI_SCHEMA,
            )
            session.add(schema_version)
            await session.flush()
            schema_version_id = schema_version.id
            session.add(
                PatientForm(
                    id=patient_form_id,
                    tenant_id=rbac_world.tenant_id,
                    schema_version_id=schema_version_id,
                    patient_name="plan test patient",
                    intake_payload={"patient_information": {"patient_name": "Plan Test Patient"}},
                )
            )

        yield patient_form_id

        async with sessionmaker() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM call_event WHERE call_id IN "
                    "(SELECT id FROM call WHERE form_id = :fid)"
                ).bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM call WHERE form_id = :fid").bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE id = :fid").bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(sid=schema_version_id)
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_call_stashes_plan_with_raw_prefill(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    compilable_form_id: UUID,
    database_url: str,
) -> None:
    resp = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(compilable_form_id)},
    )
    assert resp.status_code == 200, resp.text
    room_name = resp.json()["data"]["room_name"]

    redis = create_redis(Settings(_env_file=None).redis_url)
    try:
        raw_plan = await redis.get(call_plan_key(room_name))
        assert raw_plan is not None, "compiled plan missing from Redis"
        plan = CallPlan.model_validate_json(raw_plan)
        assert plan.room_name == room_name
        assert "test persona" in plan.flat_instructions
        # Tokenization removed: the plan carries the RAW confirm value (dev
        # simplification — synthetic-data-only, adr/devops-todo.md #8).
        confirm = next(
            f for s in plan.sections for f in s.fields if f.field_path.endswith("patient_name")
        )
        assert confirm.mode == "confirm" and confirm.confirm_value == "Plan Test Patient"
        # There is no separate sealed-seed key anymore.
        assert await redis.get(f"vera:phiseed:{room_name}") is None
    finally:
        await redis.delete(call_plan_key(room_name))
        await redis.aclose()

    # Lineage: the call pins the exact schema version it compiled from.
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            call = (
                await session.execute(select(Call).where(Call.form_id == compilable_form_id))
            ).scalar_one()
            plan_schema_version = UUID(plan.schema_version_id)
            form_version = (
                await session.execute(
                    select(PatientForm.schema_version_id).where(
                        PatientForm.id == compilable_form_id
                    )
                )
            ).scalar_one()
            assert plan_schema_version == form_version
            assert str(call.id) == plan.call_id
    finally:
        await engine.dispose()
