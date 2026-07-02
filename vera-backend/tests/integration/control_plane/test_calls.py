"""Integration tests for the /calls endpoints.

All three endpoints are tested against a live RLS-enforcing Postgres
connection with a FakeLiveKit injected in the authz_app fixture (see conftest.py).
"""

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models import PatientForm
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType
from vera_core.observability.correlation import parse_room_name


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# seeded_form_id — a PatientForm row owned by rbac_world.tenant_id.
# PatientForm requires a schema_version_id (FK RESTRICT) so we must seed a
# FormSchema + SchemaVersion first using a superuser (BYPASSRLS) session.
# We build a fresh sessionmaker from `database_url` directly.
#
# Function-scoped (not session) on purpose: `form_schema.insurance_type` is a
# globally UNIQUE, CHECK-constrained catalog key (only INFERTILITY_TREATMENT is
# valid), and `tests/integration/db/test_seed_form_schemas.py` wipes + seeds that
# same family. A session-scoped row would persist into those tests and collide on
# the UNIQUE constraint / FK. Creating and tearing down per-test keeps the row
# alive only for the duration of one test, so the two suites never contend.
#
# The fixture yields the UUID then cleans up call_event, call, patient_form,
# schema_version, and form_schema rows so the rbac_world teardown can delete
# the tenant without hitting the FK on patient_form.
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_form_id(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """Insert a minimal FormSchema → SchemaVersion → PatientForm chain and
    yield the PatientForm.id.  Cleans up on teardown so rbac_world can
    delete the tenant without a FK violation."""
    form_schema_id = uuid7()
    schema_version_id = uuid7()
    patient_form_id = uuid7()

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session, session.begin():
            session.add(
                FormSchema(
                    id=form_schema_id,
                    insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                    name="Test Schema",
                )
            )
            await session.flush()
            session.add(
                SchemaVersion(
                    id=schema_version_id,
                    schema_id=form_schema_id,
                    version=1,
                    schema_json={},
                )
            )
            await session.flush()
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
        # before rbac_world deletes the tenant.
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
async def test_supervisor_token_can_list_calls(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    """The second permissioned persona (SUPERVISOR) is wired and authenticated."""
    resp = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert resp.status_code == 200, resp.text


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
