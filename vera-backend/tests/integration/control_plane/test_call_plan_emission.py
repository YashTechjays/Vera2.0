"""Starting a call compiles the pinned schema into a non-PHI plan and stashes it in
the call-plan store under the room name — the artifact the worker reads."""

import json
from collections.abc import AsyncGenerator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.call_plan import InMemoryCallPlanStore
from vera_core.db import uuid7
from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import compile_document
from vera_core.models import PatientForm
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType

_PHI_NAME = "Jane PHI Doe"
_MEMBER_ID = "W88012345"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def v2_form_id(database_url: str, rbac_world: RBACWorld) -> AsyncGenerator[UUID]:
    """A PatientForm pinned to a SchemaVersion holding the real compiled IBV v2 doc,
    with a PHI patient name so the test can assert the plan never leaks it."""
    schema_json = json.loads(compile_document(SCHEMAS["infertility_treatment"][1]()))
    form_id, version_id = uuid7(), uuid7()
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
                    name="Plan Test Schema",
                )
                session.add(schema)
                await session.flush()
            session.add(
                SchemaVersion(
                    id=version_id, schema_id=schema.id, version=9999, schema_json=schema_json
                )
            )
            await session.flush()
            session.add(
                PatientForm(
                    id=form_id,
                    tenant_id=rbac_world.tenant_id,
                    schema_version_id=version_id,
                    patient_name=_PHI_NAME,
                    # intake_payload is section-nested (no `sections.` wrapper).
                    intake_payload={
                        "patient_information": {"patient_name": _PHI_NAME},
                        "insurance_information": {"policy_number": _MEMBER_ID},
                    },
                )
            )
            schema_id = schema.id

        yield form_id

        async with sessionmaker() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM call_event WHERE call_id IN "
                    "(SELECT id FROM call WHERE form_id = :fid)"
                ).bindparams(fid=form_id)
            )
            await session.execute(
                text("DELETE FROM call WHERE form_id = :fid").bindparams(fid=form_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE id = :fid").bindparams(fid=form_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :vid").bindparams(vid=version_id)
            )
            if created_schema:
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :sid").bindparams(sid=schema_id)
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_call_writes_prefilled_plan(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    v2_form_id: UUID,
    call_plan_store: InMemoryCallPlanStore,
) -> None:
    resp = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(v2_form_id)},
    )
    assert resp.status_code == 200, resp.text
    room_name = resp.json()["data"]["room_name"]

    plan = await call_plan_store.get(room_name)
    assert plan is not None
    assert plan.schema_version == "2.1"
    assert plan.room_name == room_name
    # Prefill (from intake_payload): patient name is known context, member ID a CONFIRM read-back.
    ctx = {c.field_path: c.value for c in plan.context_knowledge}
    assert ctx.get("sections.patient_information.patient_name") == _PHI_NAME
    member_id = next(
        f
        for t in plan.tasks
        for f in t.fields
        if f.field_path == "sections.insurance_information.policy_number"
    )
    assert member_id.status == "CONFIRM" and member_id.prefilled_value == _MEMBER_ID
    # The ask-fields are still compiled as COLLECT.
    collect = {f.field_path for t in plan.tasks for f in t.fields if f.status == "COLLECT"}
    assert "sections.infertility_treatment.infertility_tx_covered" in collect
