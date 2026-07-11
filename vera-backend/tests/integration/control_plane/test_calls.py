"""Integration tests for the /calls endpoints.

Every endpoint is exercised against a live RLS-enforcing Postgres
connection with a FakeLiveKit injected in the authz_app fixture (see conftest.py).
"""

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import FakeLiveKit, RBACWorld
from vera_core.db import uuid7
from vera_core.models import (
    AppUser,
    AuditLog,
    Call,
    InsuranceProvider,
    InterventionEvent,
    PatientForm,
)
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType
from vera_core.observability.correlation import parse_room_name


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
    fake_livekit: FakeLiveKit,
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
    # Watch-only by default: the token must not allow publishing audio.
    assert fake_livekit.minted[-1][2] is False

    talk = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert talk.status_code == 200, talk.text
    assert fake_livekit.minted[-1][2] is True


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
async def test_create_call_unknown_provider_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    # Checked before flush so an unknown id is a 404, not an FK violation → 500.
    resp = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id), "insurance_provider_id": str(uuid4())},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_create_call_inactive_provider_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # An inactive provider must not start a call (nor let its playbook steer one) — the
    # existence check requires status = active, so it 404s like an unknown id.
    provider_id = uuid7()
    async with admin_sessionmaker() as s, s.begin():
        s.add(
            InsuranceProvider(id=provider_id, name=f"Inactive {provider_id.hex}", status="inactive")
        )
    try:
        resp = await client.post(
            "/api/v1/calls",
            headers=_auth(rbac_world.admin_token),
            json={"form_id": str(seeded_form_id), "insurance_provider_id": str(provider_id)},
        )
        assert resp.status_code == 404, resp.text
    finally:
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(
                text("DELETE FROM insurance_provider WHERE id = :i").bindparams(i=provider_id)
            )


@pytest.mark.asyncio
async def test_create_call_nests_persona_tweak_in_dispatch_metadata(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The tweak rides under its own metadata key so sibling keys (enable_ivr_navigation, …)
    never trip the worker's extra="forbid" PersonaTweak validation and silently drop it."""
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE tenant SET persona_tweak = CAST(:p AS jsonb) WHERE id = :t").bindparams(
                p='{"greeting": "Hello from Acme."}', t=rbac_world.tenant_id
            )
        )
    try:
        resp = await client.post(
            "/api/v1/calls",
            headers=_auth(rbac_world.admin_token),
            json={"form_id": str(seeded_form_id), "enable_ivr_navigation": True},
        )
        assert resp.status_code == 200, resp.text
        meta = fake_livekit.dispatch_metadata[-1]
        assert meta is not None
        assert meta["persona_tweak"] == {"greeting": "Hello from Acme."}
        assert meta["enable_ivr_navigation"] is True
    finally:
        # persona_tweak is JSONB NOT NULL; the untouched default is the empty object.
        async with admin_sessionmaker() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE tenant SET persona_tweak = CAST('{}' AS jsonb) WHERE id = :t"
                ).bindparams(t=rbac_world.tenant_id)
            )


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


@pytest.mark.asyncio
async def test_new_call_is_private_by_default(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = UUID(created.json()["data"]["id"])
    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.published is False
    assert row.published_at is None


@pytest.mark.asyncio
async def test_create_call_summary_reports_owner_and_private(
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
    data = created.json()["data"]
    assert data["published"] is False
    assert data["is_owner"] is True


@pytest.mark.asyncio
async def test_list_scopes_to_owner_or_published(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]

    # A non-owner (supervisor) does NOT see the admin's private call.
    before = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert all(c["id"] != call_id for c in before.json()["data"])

    # Flip published directly in the DB (publish endpoint is Task 6).
    await admin_session.execute(update(Call).where(Call.id == UUID(call_id)).values(published=True))
    await admin_session.commit()

    after = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert any(c["id"] == call_id for c in after.json()["data"])
    # And the owner still sees their own call.
    owner = await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))
    assert any(c["id"] == call_id for c in owner.json()["data"])


@pytest.mark.asyncio
async def test_list_calls_sets_no_store_and_audits_phi_disclosure(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_session: AsyncSession,
) -> None:
    resp = await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.headers["Cache-Control"] == "no-store"

    row = (
        (
            await admin_session.execute(
                select(AuditLog).where(
                    AuditLog.event_type == "phi.access",
                    AuditLog.resource_type == "call",
                    AuditLog.resource_id == "list",
                    AuditLog.actor_user_id == rbac_world.admin_id,
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.detail == {"fields": ["patient_name"]}


@pytest.mark.asyncio
async def test_ownerless_call_is_tenant_visible_and_joinable(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
) -> None:
    """A call with no owner (legacy dispatcher row) must not become invisible:
    it is tenant-visible and joinable like a published call, but unpublishable
    (there is no owner to publish it)."""
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]

    # Simulate a pre-ownership dispatcher call: strip the owner in the DB.
    await admin_session.execute(
        update(Call).where(Call.id == UUID(call_id)).values(initiated_by_id=None)
    )
    await admin_session.commit()

    listed = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    row = next((c for c in listed.json()["data"] if c["id"] == call_id), None)
    assert row is not None
    assert row["is_owner"] is False
    assert row["published"] is False

    join = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert join.status_code == 200, join.text

    pub = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert pub.status_code == 403


@pytest.mark.asyncio
async def test_publish_is_owner_only_idempotent_and_audited(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]

    # Non-owner with calls:publish (supervisor) cannot publish someone else's call.
    forbidden = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.supervisor_token)
    )
    assert forbidden.status_code == 403, forbidden.text

    # No-permission user is rejected too.
    norole = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.norole_token)
    )
    assert norole.status_code == 403, norole.text

    async def publish_audit_rows() -> list[AuditLog]:
        result = await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.publish", AuditLog.resource_id == call_id
            )
        )
        return list(result.scalars().all())

    # Owner publishes.
    pub = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["data"]["published"] is True
    # Same row shape as list_calls — None would blank the UI's Patient cell.
    assert pub.json()["data"]["patient_name"] == "Test Patient"

    # Exactly one publish audit row exists.
    assert len(await publish_audit_rows()) == 1

    # Idempotent: a second publish is a no-op and adds no audit row.
    again = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert again.status_code == 200
    assert len(await publish_audit_rows()) == 1


@pytest.mark.asyncio
async def test_join_token_gated_and_audited_for_non_owner(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]

    # Non-owner on a PRIVATE call: 404 (existence not revealed).
    private = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert private.status_code == 404, private.text

    # Owner publishes.
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert published.status_code == 200, published.text

    # Non-owner on a PUBLISHED call: token + one join audit row.
    joined = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert joined.status_code == 200, joined.text
    assert joined.json()["data"]["token"].startswith("faketoken:")

    result = await admin_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == "call.intervene.join", AuditLog.resource_id == call_id
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_intervene_token_requires_calls_intervene(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    fake_livekit: FakeLiveKit,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert published.status_code == 200, published.text

    # calls:read alone still allows watching — server-side muted token.
    watch = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.listener_token)
    )
    assert watch.status_code == 200, watch.text
    assert fake_livekit.minted[-1][2] is False

    # ...but never publishing.
    denied = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.listener_token),
    )
    assert denied.status_code == 403, denied.text
    assert "calls:intervene" in denied.json()["message"]

    result = await admin_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == "authz.deny",
            AuditLog.resource_id == call_id,
            AuditLog.permission_key == "calls:intervene",
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_owner_intervene_requires_permission_too(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    # The listener owns the call (start only needs calls:read) — ownership does
    # not substitute for calls:intervene.
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.listener_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]

    watch = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.listener_token)
    )
    assert watch.status_code == 200, watch.text

    denied = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.listener_token),
    )
    assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
async def test_intervene_on_private_call_stays_404_for_non_owner(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    # Visibility beats capability: holding calls:intervene must not turn a
    # private call's 404 into a 403 (no enumeration).
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]

    denied = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert denied.status_code == 404, denied.text


@pytest.mark.asyncio
async def test_supervisor_can_intervene_on_published_call(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert published.status_code == 200, published.text

    joined = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert joined.status_code == 200, joined.text
    assert fake_livekit.minted[-1][2] is True


async def _create_published_call(
    client: httpx.AsyncClient, rbac_world: RBACWorld, form_id: UUID
) -> str:
    """Admin-owned, published call — the canonical setup for intervene tests."""
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(form_id)},
    )
    assert created.status_code == 200, created.text
    call_id: str = created.json()["data"]["id"]
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert published.status_code == 200, published.text
    return call_id


async def _intervention_events(session: AsyncSession, call_id: str) -> list[InterventionEvent]:
    result = await session.execute(
        select(InterventionEvent).where(InterventionEvent.call_id == UUID(call_id))
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_intervene_claims_lock_and_writes_intervention_event(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _create_published_call(client, rbac_world, seeded_form_id)

    joined = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert joined.status_code == 200, joined.text

    # Lock claimed by the supervisor, with a DB-clock claim time.
    call = (await admin_session.execute(select(Call).where(Call.id == UUID(call_id)))).scalar_one()
    assert call.intervener_user_id == rbac_world.supervisor_id
    assert call.intervener_claimed_at is not None

    # Exactly one takeover row in the purpose-built intervention audit table.
    events = await _intervention_events(admin_session, call_id)
    assert len(events) == 1
    assert events[0].type == "takeover"
    assert events[0].supervisor_id == rbac_world.supervisor_id

    # The token carries the supervisor's email + intervener mode for the room UI.
    minted = fake_livekit.minted[-1]
    assert minted.can_publish is True
    assert minted.name == "supervisor@test.example"
    assert minted.attributes == {"vera.mode": "intervener"}

    # The join audit row records that this join was an intervention.
    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.intervene.join", AuditLog.resource_id == call_id
            )
        )
    ).scalars()
    details = [row.detail for row in rows]
    assert any(d.get("intervene") is True for d in details)


@pytest.mark.asyncio
async def test_second_intervener_conflicts_within_grace(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
) -> None:
    call_id = await _create_published_call(client, rbac_world, seeded_form_id)

    first = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert first.status_code == 200, first.text

    # A fresh claim is inside the connect-grace window: no LiveKit staleness
    # probe (fake_livekit.participants is empty — a probe would allow a steal),
    # the second caller is refused outright.
    second = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_stale_lock_is_stolen_when_holder_absent(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _create_published_call(client, rbac_world, seeded_form_id)
    first = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert first.status_code == 200, first.text

    # Age the claim past the grace window; the holder is NOT in the room
    # (closed their tab before/after connecting) → the lock is stale.
    await admin_session.execute(
        update(Call)
        .where(Call.id == UUID(call_id))
        .values(intervener_claimed_at=text("now() - interval '5 minutes'"))
    )
    await admin_session.commit()

    room_name = (
        await client.get(
            f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.admin_token)
        )
    ).json()["data"]["room_name"]
    fake_livekit.participants[room_name] = []

    stolen = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert stolen.status_code == 200, stolen.text

    call = (await admin_session.execute(select(Call).where(Call.id == UUID(call_id)))).scalar_one()
    assert call.intervener_user_id == rbac_world.admin_id

    # Both claims are recorded; the steal's join audit names the released holder.
    assert len(await _intervention_events(admin_session, call_id)) == 2
    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.intervene.join", AuditLog.resource_id == call_id
            )
        )
    ).scalars()
    assert any(
        row.detail.get("stale_lock_released") == str(rbac_world.supervisor_id) for row in rows
    )


@pytest.mark.asyncio
async def test_stale_check_respects_present_holder(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _create_published_call(client, rbac_world, seeded_form_id)
    first = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert first.status_code == 200, first.text

    await admin_session.execute(
        update(Call)
        .where(Call.id == UUID(call_id))
        .values(intervener_claimed_at=text("now() - interval '5 minutes'"))
    )
    await admin_session.commit()

    room_name = (
        await client.get(
            f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.admin_token)
        )
    ).json()["data"]["room_name"]
    # The holder is still connected — an old claim is not a stale claim.
    fake_livekit.participants[room_name] = [f"supervisor-{rbac_world.supervisor_id}"]

    refused = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert refused.status_code == 409, refused.text


@pytest.mark.asyncio
async def test_self_reclaim_is_idempotent(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
) -> None:
    call_id = await _create_published_call(client, rbac_world, seeded_form_id)

    for _ in range(2):  # tab refresh: the holder re-requests an intervene token
        joined = await client.get(
            f"/api/v1/calls/{call_id}/join-token?intervene=true",
            headers=_auth(rbac_world.supervisor_token),
        )
        assert joined.status_code == 200, joined.text

    # A reconnect is not a new intervention — still exactly one takeover row.
    assert len(await _intervention_events(admin_session, call_id)) == 1


@pytest.mark.asyncio
async def test_intervene_on_terminal_call_conflicts(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
) -> None:
    call_id = await _create_published_call(client, rbac_world, seeded_form_id)
    await admin_session.execute(
        update(Call).where(Call.id == UUID(call_id)).values(current_status="completed")
    )
    await admin_session.commit()

    ended = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert ended.status_code == 409, ended.text


@pytest.mark.asyncio
async def test_listen_token_carries_listener_mode(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _create_published_call(client, rbac_world, seeded_form_id)

    watched = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert watched.status_code == 200, watched.text

    minted = fake_livekit.minted[-1]
    assert minted.can_publish is False
    assert minted.name == "supervisor@test.example"
    assert minted.attributes == {"vera.mode": "listener"}


@pytest.mark.asyncio
async def test_owner_revokes_intervener_access(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert published.status_code == 200, published.text

    # Non-owner cannot revoke.
    forbidden = await client.post(
        f"/api/v1/calls/{call_id}/revoke-access",
        headers=_auth(rbac_world.supervisor_token),
        json={"target_user_id": str(uuid4())},
    )
    assert forbidden.status_code == 403, forbidden.text

    # A published call is joinable by the supervisor before the revoke.
    ok_join = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert ok_join.status_code == 200, ok_join.text

    # Owner revokes the supervisor; LiveKit removal is invoked and audited.
    target = (
        await admin_session.execute(
            select(AppUser.id).where(
                AppUser.email == "supervisor@test.example",
                AppUser.tenant_id == rbac_world.tenant_id,
            )
        )
    ).scalar_one()
    revoked = await client.post(
        f"/api/v1/calls/{call_id}/revoke-access",
        headers=_auth(rbac_world.admin_token),
        json={"target_user_id": str(target)},
    )
    assert revoked.status_code == 200, revoked.text
    assert any(ident == f"supervisor-{target}" for _room, ident in fake_livekit.removed)

    result = await admin_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == "call.intervene.revoke", AuditLog.resource_id == call_id
        )
    )
    assert len(result.scalars().all()) == 1

    # The revocation is durable: no fresh join token, even though still published.
    denied = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert denied.status_code == 404, denied.text
    row = (await admin_session.execute(select(Call).where(Call.id == UUID(call_id)))).scalar_one()
    assert row.published is True
    assert row.revoked_user_ids == [str(target)]


@pytest.mark.asyncio
async def test_owner_revoke_of_departed_intervener_is_noop_but_audited(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
) -> None:
    created = await client.post(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
        json={"form_id": str(seeded_form_id)},
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["data"]["id"]
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert published.status_code == 200, published.text

    # The intervener already left / never joined — LiveKit reports not_found.
    fake_livekit.remove_not_found = True

    revoked = await client.post(
        f"/api/v1/calls/{call_id}/revoke-access",
        headers=_auth(rbac_world.admin_token),
        json={"target_user_id": str(uuid4())},
    )
    assert revoked.status_code == 200, revoked.text

    result = await admin_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == "call.intervene.revoke", AuditLog.resource_id == call_id
        )
    )
    assert len(result.scalars().all()) == 1
