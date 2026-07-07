"""Integration tests for the Voice Lab session endpoint.

No DB rows are written (the endpoint is persistence-free); the FakeLiveKit
injected by the authz_app fixture records room/dispatch/SIP calls so we assert on
the seam without a real LiveKit server.
"""

import json
from collections.abc import AsyncGenerator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import FakeLiveKit, RBACWorld
from vera_core.call_plan import InMemoryCallPlanStore
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import compile_document
from vera_core.integrations.credentials import seal_credentials
from vera_core.models import Integration, IntegrationType, PatientForm
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.observability.correlation import parse_room_name, room_name_for_call

_TRUNK_TYPE = "livekit_outbound_trunk_id"
_TRUNK_VALUE = "ST_test_trunk"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def trunk_configured(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    trunk_integration_type: None,
) -> None:
    """Seal a trunk credential for the test tenant so the outbound dial resolves it from
    the DB. Uses the same LocalDevKMS master key as the app under test, so the app's
    get_integration_credentials can open what we seal here. The `trunk_integration_type`
    fixture owns the catalog-type row and tears down the Integration we add below."""
    kms = LocalDevKMS(master_key=b"a" * 32)
    async with admin_sessionmaker() as session, session.begin():
        type_id = (
            await session.execute(
                select(IntegrationType.id).where(IntegrationType.name == _TRUNK_TYPE)
            )
        ).scalar_one()
        integration = Integration(
            tenant_id=rbac_world.tenant_id,
            integration_type_id=type_id,
            status="active",
        )
        await seal_credentials(kms, integration=integration, credentials={"trunk_id": _TRUNK_VALUE})
        session.add(integration)


@pytest.mark.asyncio
async def test_browser_session_returns_caller_token_with_wait_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    before = len(fake_livekit.created)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["mode"] == "browser"
    assert parse_room_name(body["room_name"]) is not None
    # browser speaker identity + wait_for_speaker dispatch metadata
    assert body["token"].startswith(f"faketoken:{body['room_name']}:caller-")
    assert fake_livekit.created[before] == body["room_name"]
    assert fake_livekit.dispatch_metadata[before] == {
        "wait_for_speaker": True,
        "publish_transcript": True,
        "enable_ivr_navigation": False,
    }


@pytest.fixture
async def published_infertility_schema(database_url: str) -> AsyncGenerator[None]:
    """Guarantee a PUBLISHED infertility_treatment v2 schema exists (find-or-create the
    global catalog rows, reusing a seeded one if present). Self-contained so the test
    doesn't depend on ambient seed state; cleans up only what it created."""
    schema_json = json.loads(compile_document(SCHEMAS["infertility_treatment"][1]()))
    version_id = uuid7()
    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    created_schema = created_version = False
    schema_id = None
    try:
        async with sessionmaker() as s, s.begin():
            schema = (
                await s.execute(
                    select(FormSchema).where(FormSchema.insurance_type == "infertility_treatment")
                )
            ).scalar_one_or_none()
            if schema is None:
                schema = FormSchema(
                    id=uuid7(), insurance_type="infertility_treatment", name="Infertility"
                )
                s.add(schema)
                await s.flush()
                created_schema = True
            schema_id = schema.id
            has_published = (
                await s.execute(
                    select(SchemaVersion.id).where(
                        SchemaVersion.schema_id == schema_id, SchemaVersion.status == "published"
                    )
                )
            ).scalar_one_or_none()
            if has_published is None:
                next_version = (
                    await s.execute(
                        select(func.coalesce(func.max(SchemaVersion.version), 0) + 1).where(
                            SchemaVersion.schema_id == schema_id
                        )
                    )
                ).scalar_one()
                s.add(
                    SchemaVersion(
                        id=version_id,
                        schema_id=schema_id,
                        version=next_version,
                        schema_json=schema_json,
                        status="published",
                    )
                )
                created_version = True
        yield
        async with sessionmaker() as s, s.begin():
            if created_version:
                await s.execute(
                    text("DELETE FROM schema_version WHERE id = :v").bindparams(v=version_id)
                )
            if created_schema:
                await s.execute(
                    text("DELETE FROM form_schema WHERE id = :s").bindparams(s=schema_id)
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_browser_session_writes_plan(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    call_plan_store: InMemoryCallPlanStore,
    published_infertility_schema: None,
) -> None:
    # A Voice Lab browser session compiles the published infertility v2 schema into a plan
    # (PHI-free) so the worker drives the plan agent, not the static fallback.
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser"},
    )
    assert resp.status_code == 200, resp.text
    room_name = resp.json()["data"]["room_name"]

    plan = await call_plan_store.get(room_name)
    assert plan is not None
    assert plan.schema_version == "2.1"
    assert plan.room_name == room_name
    assert "sections.infertility_treatment.infertility_tx_covered" in {
        f.field_path for t in plan.tasks for f in t.fields if f.status == "COLLECT"
    }


_FORM_PHI_NAME = "Jane VoiceLab Doe"


@pytest.fixture
async def infertility_patient_form(
    database_url: str, rbac_world: RBACWorld, published_infertility_schema: None
) -> AsyncGenerator[UUID]:
    """A PatientForm (with a PHI name) pinned to the published infertility schema, owned by
    the test tenant — so a form_id session compiles from that form's schema."""
    form_id = uuid7()
    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as s, s.begin():
            version_id = (
                await s.execute(
                    select(SchemaVersion.id)
                    .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                    .where(
                        FormSchema.insurance_type == "infertility_treatment",
                        SchemaVersion.status == "published",
                    )
                )
            ).scalar_one()
            s.add(
                PatientForm(
                    id=form_id,
                    tenant_id=rbac_world.tenant_id,
                    schema_version_id=version_id,
                    patient_name=_FORM_PHI_NAME,
                )
            )
        yield form_id
        async with sessionmaker() as s, s.begin():
            await s.execute(
                text("DELETE FROM patient_form WHERE id = :fid").bindparams(fid=form_id)
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_browser_session_with_form_id_writes_plan(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    call_plan_store: InMemoryCallPlanStore,
    infertility_patient_form: UUID,
) -> None:
    # A selected patient form compiles the plan from that form's pinned schema — PHI-free.
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser", "form_id": str(infertility_patient_form)},
    )
    assert resp.status_code == 200, resp.text
    room_name = resp.json()["data"]["room_name"]

    plan = await call_plan_store.get(room_name)
    assert plan is not None
    assert plan.schema_version == "2.1"
    assert _FORM_PHI_NAME not in plan.model_dump_json()


@pytest.mark.asyncio
async def test_unknown_form_id_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser", "form_id": str(uuid7())},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_ivr_navigation_flag_rides_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    before = len(fake_livekit.created)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser", "enable_ivr_navigation": True},
    )
    assert resp.status_code == 200, resp.text
    meta = fake_livekit.dispatch_metadata[before]
    assert meta is not None
    assert meta["enable_ivr_navigation"] is True


@pytest.mark.asyncio
async def test_outbound_without_trunk_configured_returns_409(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "+15551234567"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_outbound_with_invalid_phone_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    trunk_configured: None,
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "not-a-number"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_outbound_with_trunk_and_valid_phone_places_sip_call(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_configured: None,
) -> None:
    before = len(fake_livekit.sip_calls)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "+15551234567"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["mode"] == "outbound"
    # listen-only monitor identity for the browser
    assert body["token"].startswith(f"faketoken:{body['room_name']}:monitor-")
    assert fake_livekit.sip_calls[before] == (body["room_name"], "+15551234567", _TRUNK_VALUE)


@pytest.mark.asyncio
async def test_outbound_dial_failure_returns_502_and_tears_down_room(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
    trunk_configured: None,
) -> None:
    # The trunk is stored (passed save-time validation) but the dial fails at the
    # provider seam — e.g. the trunk was deleted afterwards. Expect a clean 502, not a
    # 500, and the room we created must be torn down so no agent is left orphaned.
    fake_livekit.dial_error = True
    before_deleted = len(fake_livekit.deleted)
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "outbound", "phone_number": "+15551234567"},
    )
    assert resp.status_code == 502, resp.text
    assert len(fake_livekit.deleted) == before_deleted + 1


@pytest.mark.asyncio
async def test_end_session_deletes_the_room(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    started = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.admin_token),
        json={"mode": "browser"},
    )
    room_name = started.json()["data"]["room_name"]

    before = len(fake_livekit.deleted)
    resp = await client.delete(
        f"/api/v1/voice-lab/sessions/{room_name}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert fake_livekit.deleted[before] == room_name


@pytest.mark.asyncio
async def test_end_session_foreign_tenant_room_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    # A room name carrying another tenant's uuid must not be deletable.
    foreign_room = room_name_for_call(rbac_world.other_tenant_id, uuid7())
    resp = await client.delete(
        f"/api/v1/voice-lab/sessions/{foreign_room}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_end_session_malformed_room_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.delete(
        "/api/v1/voice-lab/sessions/not-a-room",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_voice_lab_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/voice-lab/sessions", json={"mode": "browser"})
    assert resp.status_code == 401

    ended = await client.delete("/api/v1/voice-lab/sessions/call--x--y")
    assert ended.status_code == 401
