"""Integration tests for the /calls endpoints — live RLS Postgres, FakeLiveKit (conftest.py)."""

import json
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.api.v1.calls import (
    _INTERVENE_CONNECT_GRACE,
    _LIVE_TAIL_FIRST_ENTRY_DEADLINE_S,
)
from tests.integration.control_plane.conftest import (
    FakeLiveKit,
    RBACWorld,
    _MemCallStreamStore,
    seed_call,
)
from vera_core.call_stream import CallStreamService
from vera_core.db import uuid7
from vera_core.db.rls import tenant_session
from vera_core.models import AuditLog, Call, InterventionEvent, PatientForm, Transcript
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import CallStatus, InsuranceType
from vera_core.observability.correlation import parse_room_name, room_name_for_call


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def seeded_form_id(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """A PatientForm owned by rbac_world.tenant_id. The INFERTILITY_TREATMENT
    FormSchema is find-or-create (insurance_type is globally UNIQUE and CI already
    seeds it); teardown drops only rows this fixture created so the shared schema
    survives and rbac_world can delete the tenant without a FK violation."""
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

        # Teardown: drop this form's calls (and their events) before rbac_world
        # deletes the tenant; drop the schema chain only if we created it.
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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )

    lst = await client.get(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
    )
    assert lst.status_code == 200, lst.text
    row = next((c for c in lst.json()["data"] if c["id"] == str(call_id)), None)
    assert row is not None
    assert row["status"] == "initiated"
    assert parse_room_name(row["room_name"]) is not None
    assert "insurance_provider" in row
    # seeded_form_id binds an INFERTILITY_TREATMENT schema — the join must surface it.
    assert row["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value


@pytest.mark.asyncio
async def test_join_token_returns_room_scoped_token(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)

    tok = await client.get(
        f"/api/v1/calls/{call_id}/join-token",
        headers=_auth(rbac_world.admin_token),
    )
    assert tok.status_code == 200, tok.text
    assert tok.headers["cache-control"] == "no-store"  # token + email, never cache
    body = tok.json()["data"]
    assert body["room_name"] == room
    assert body["token"].startswith("faketoken:")
    # Watch-only by default: no publish.
    assert fake_livekit.minted[-1][2] is False

    talk = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert talk.status_code == 200, talk.text
    assert fake_livekit.minted[-1][2] is True

    # The audit event names the mode: watch-only is listen-only, publish is intervene.
    result = await admin_session.execute(
        select(AuditLog)
        .where(AuditLog.resource_type == "call", AuditLog.resource_id == str(call_id))
        .order_by(AuditLog.created_at)
    )
    events = [row.event_type for row in result.scalars().all()]
    assert events == ["call.listen-only.join", "call.intervene.join"]


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
async def test_virtual_assistant_can_list_and_publish_calls(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """VA now holds calls:read/calls:publish (Live Monitoring page access)."""
    listed = await client.get("/api/v1/calls", headers=_auth(rbac_world.virtual_assistant_token))
    assert listed.status_code == 200, listed.text

    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.virtual_assistant_id,
    )
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish",
        headers=_auth(rbac_world.virtual_assistant_token),
    )
    assert published.status_code == 200, published.text


@pytest.mark.asyncio
async def test_calls_require_auth(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    """All endpoints deny unauthenticated callers."""
    resp_list = await client.get("/api/v1/calls")
    assert resp_list.status_code == 401

    resp_token = await client.get(f"/api/v1/calls/{uuid4()}/join-token")
    assert resp_token.status_code == 401

    resp_events = await client.get(f"/api/v1/calls/{uuid4()}/events")
    assert resp_events.status_code == 401


@pytest.mark.asyncio
async def test_new_call_is_private_by_default(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.published is False
    assert row.published_at is None


@pytest.mark.asyncio
async def test_list_scopes_to_owner_or_published(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    call_id_str = str(call_id)

    before = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert all(c["id"] != call_id_str for c in before.json()["data"])

    await admin_session.execute(update(Call).where(Call.id == call_id).values(published=True))
    await admin_session.commit()

    after = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    assert any(c["id"] == call_id_str for c in after.json()["data"])
    owner = await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))
    assert any(c["id"] == call_id_str for c in owner.json()["data"])


@pytest.mark.asyncio
async def test_list_calls_history_scope_returns_terminal_calls_only(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    live_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.ACTIVE.value,
    )
    done_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.COMPLETED.value,
    )

    history = await client.get(
        "/api/v1/calls",
        params={"scope": "history"},
        headers=_auth(rbac_world.admin_token),
    )
    assert history.status_code == 200, history.text
    ids = [c["id"] for c in history.json()["data"]]
    assert str(done_id) in ids
    assert str(live_id) not in ids
    # Summaries carry ended_at so the UI can render a fixed duration.
    assert "ended_at" in history.json()["data"][0]

    live = await client.get("/api/v1/calls", headers=_auth(rbac_world.admin_token))
    live_ids = [c["id"] for c in live.json()["data"]]
    assert str(live_id) in live_ids
    assert str(done_id) not in live_ids


@pytest.mark.asyncio
async def test_call_stats_counts_todays_visible_calls(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """total_today counts today's calls (any status), live/critical the in-flight
    ones — all restricted to what the caller could see in the list (a stranger's
    private call is invisible to the stats too). One in-flight call max per form
    (uq_call_active_form), so today's set is 1 critical + 2 completed."""
    for status in (CallStatus.CRITICAL, CallStatus.COMPLETED, CallStatus.COMPLETED):
        await seed_call(
            admin_sessionmaker,
            rbac_world.tenant_id,
            seeded_form_id,
            initiated_by_id=rbac_world.admin_id,
            status=status.value,
        )
    # An old call must not count toward today.
    old_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.COMPLETED.value,
    )
    await admin_session.execute(
        update(Call).where(Call.id == old_id).values(created_at=text("now() - interval '2 days'"))
    )
    await admin_session.commit()

    resp = await client.get("/api/v1/calls/stats", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("cache-control") == "no-store"
    data = resp.json()["data"]
    assert data == {"total_today": 3, "live": 1, "critical": 1}

    # The supervisor sees none of the admin's private calls.
    other = await client.get("/api/v1/calls/stats", headers=_auth(rbac_world.supervisor_token))
    assert other.json()["data"] == {"total_today": 0, "live": 0, "critical": 0}


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
    # health_reason (analyzer justification, PHI) is disclosed on the list rows
    # alongside patient_name and insurance_provider — all audited by field name.
    assert row.detail == {"fields": ["patient_name", "insurance_provider", "health_reason"]}


@pytest.mark.asyncio
async def test_ownerless_call_is_tenant_visible_and_joinable(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An ownerless (dispatcher) call is tenant-visible and joinable like a
    published call, but unpublishable — there is no owner to publish it."""
    call_id = await seed_call(admin_sessionmaker, rbac_world.tenant_id, seeded_form_id)
    call_id_str = str(call_id)

    listed = await client.get("/api/v1/calls", headers=_auth(rbac_world.supervisor_token))
    row = next((c for c in listed.json()["data"] if c["id"] == call_id_str), None)
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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )

    # Non-owner with calls:publish (supervisor) cannot publish someone else's call.
    forbidden = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.supervisor_token)
    )
    assert forbidden.status_code == 403, forbidden.text

    norole = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.norole_token)
    )
    assert norole.status_code == 403, norole.text

    async def publish_audit_rows() -> list[AuditLog]:
        result = await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.publish", AuditLog.resource_id == str(call_id)
            )
        )
        return list(result.scalars().all())

    pub = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["data"]["published"] is True
    # Same row shape as list_calls — None would blank the UI's Patient cell.
    assert pub.json()["data"]["patient_name"] == "Test Patient"
    assert pub.json()["data"]["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value

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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )

    # Non-owner on a PRIVATE call: 404 (existence not revealed).
    private = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert private.status_code == 404, private.text

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
            AuditLog.event_type == "call.listen-only.join", AuditLog.resource_id == str(call_id)
        )
    )
    assert len(result.scalars().all()) == 1


# ---------------------------------------------------------------------------
# GET /calls/{call_id}/events — live envelope SSE
# ---------------------------------------------------------------------------


async def _events_audit_rows(
    admin_session: AsyncSession, call_id: UUID, *, decision: str
) -> list[AuditLog]:
    result = await admin_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == "phi.access",
            AuditLog.resource_type == "call_events",
            AuditLog.resource_id == str(call_id),
            AuditLog.decision == decision,
        )
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_call_events_streams_envelope_frames_for_owner(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    await call_stream_service.publish_turn(room, "agent", "hello", ts=1)
    await call_stream_service.end(room)

    resp = await client.get(
        f"/api/v1/calls/{call_id}/events", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-accel-buffering"] == "no"

    # Each SSE frame is "id: <entry>\ndata: <json>" where the JSON is a CallStreamEvent envelope.
    frames = [f for f in resp.text.split("\n\n") if f]
    assert frames, resp.text
    id_line, data_line = frames[0].split("\n")
    assert id_line.startswith("id: ")
    assert data_line.startswith("data: ")
    envelope = json.loads(data_line.removeprefix("data: "))
    assert envelope["type"] == "transcript"
    assert envelope["data"] == {"role": "agent", "source": "bot", "text": "hello"}
    assert isinstance(envelope["ts"], int)

    assert len(await _events_audit_rows(admin_session, call_id, decision="allow")) == 1


@pytest.mark.asyncio
async def test_call_events_visible_call_without_permission_403_and_audited_deny(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A VISIBLE (ownerless) call without calls:read is 403, not 404 (visibility
    already passed), and the deny is audited."""
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=None,  # ownerless → tenant-visible, like join-token
    )
    resp = await client.get(
        f"/api/v1/calls/{call_id}/events", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403, resp.text
    assert len(await _events_audit_rows(admin_session, call_id, decision="deny")) == 1


@pytest.mark.asyncio
async def test_call_events_hidden_for_private_call_non_owner(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    resp = await client.get(
        f"/api/v1/calls/{call_id}/events", headers=_auth(rbac_world.supervisor_token)
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_call_events_unknown_call_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
) -> None:
    resp = await client.get(
        f"/api/v1/calls/{uuid4()}/events", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_call_events_terminal_call_no_stream_serves_db_transcript_then_closes(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
) -> None:
    """Terminal call, no live stream (deleted at closeout): the DB Transcript rows
    are replayed as the same envelope frame shape, then the stream closes."""
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.COMPLETED.value,
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    assert await call_stream_service.exists(room) is False  # no stream for this room

    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as session:
        session.add_all(
            [
                Transcript(
                    tenant_id=rbac_world.tenant_id,
                    call_id=call_id,
                    seq=0,
                    source="bot",
                    role="",  # blank role -> falls back to source-derived (bot -> agent)
                    message="Hello, how can I help?",
                    spoke_at=None,
                ),
                Transcript(
                    tenant_id=rbac_world.tenant_id,
                    call_id=call_id,
                    seq=1,
                    source="rep",
                    role="user",  # explicit role wins over source-derived
                    message="I need a claim status.",
                    spoke_at=None,
                ),
                Transcript(
                    tenant_id=rbac_world.tenant_id,
                    call_id=call_id,
                    seq=2,
                    source="bot",
                    role="dtmf",  # a keypad press: bot-attributed, non-speech role
                    message="3",
                    spoke_at=None,
                ),
            ]
        )

    resp = await client.get(
        f"/api/v1/calls/{call_id}/events", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = [f for f in resp.text.split("\n\n") if f]
    assert len(frames) == 4, resp.text

    id0, data0 = frames[0].split("\n")
    assert id0 == "id: db-0"
    envelope0 = json.loads(data0.removeprefix("data: "))
    assert envelope0 == {
        "type": "transcript",
        "data": {"role": "agent", "source": "bot", "text": "Hello, how can I help?"},
        "ts": 0,
    }

    id1, data1 = frames[1].split("\n")
    assert id1 == "id: db-1"
    envelope1 = json.loads(data1.removeprefix("data: "))
    assert envelope1 == {
        "type": "transcript",
        "data": {"role": "user", "source": "rep", "text": "I need a claim status."},
        "ts": 0,
    }

    id2, data2 = frames[2].split("\n")
    assert id2 == "id: db-2"
    envelope2 = json.loads(data2.removeprefix("data: "))
    assert envelope2 == {
        "type": "transcript",
        "data": {"role": "dtmf", "source": "bot", "text": "3"},
        "ts": 0,
    }

    _id3, data3 = frames[3].split("\n")
    envelope3 = json.loads(data3.removeprefix("data: "))
    assert envelope3["type"] == "call_status"
    assert envelope3["data"] == {"status": CallStatus.COMPLETED.value}


@pytest.mark.asyncio
async def test_call_events_live_call_no_stream_terminates_at_deadline(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live call whose stream never appears must not pin the SSE open forever —
    the tail deadline (tiny here) closes it."""
    monkeypatch.setattr("control_plane.api.v1.calls._LIVE_TAIL_FIRST_ENTRY_DEADLINE_S", 0.05)
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.ACTIVE.value,
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    assert await call_stream_service.exists(room) is False

    resp = await client.get(
        f"/api/v1/calls/{call_id}/events",
        headers=_auth(rbac_world.admin_token),
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == ""  # stream never appeared: no frames, connection closes


@pytest.mark.asyncio
async def test_call_events_stream_exists_branch_still_passes_the_deadline(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    call_stream_service: CallStreamService,
    call_stream_store: _MemCallStreamStore,
) -> None:
    """Even when EXISTS said the stream is there, the tail must carry the
    first-entry deadline: the finalizer can delete the stream between EXISTS and
    the tail's first read, and a None deadline on a vanished stream would hang the
    SSE forever. Harmless for a live stream — its replay-from-0 read marks it seen
    before the deadline can fire."""
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status=CallStatus.ACTIVE.value,
    )
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    await call_stream_service.publish_turn(room, "agent", "hello", ts=1)
    await call_stream_service.end(room)
    assert await call_stream_service.exists(room) is True

    resp = await client.get(
        f"/api/v1/calls/{call_id}/events", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text

    deadlines = [d for r, d in call_stream_store.read_deadlines if r == room]
    assert deadlines == [_LIVE_TAIL_FIRST_ENTRY_DEADLINE_S]


# ---------------------------------------------------------------------------
# POST /calls/{call_id}/end — tear the LiveKit room down; the worker's
# call.ended event drives the actual closeout pipeline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_call_owner_deletes_room_and_audits(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )

    resp = await client.post(f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text

    room = room_name_for_call(rbac_world.tenant_id, call_id)
    assert room in fake_livekit.deleted

    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.end", AuditLog.resource_id == str(call_id)
            )
        )
    ).scalars()
    assert len(list(rows)) == 1

    # The endpoint never writes status — the worker's call.ended event does.
    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.current_status == "active"


@pytest.mark.asyncio
async def test_end_call_visibility_matches_join_token(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )

    # Non-owner on a PRIVATE call: 404 (existence not revealed).
    private = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.supervisor_token)
    )
    assert private.status_code == 404, private.text

    norole = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.norole_token)
    )
    assert norole.status_code == 403, norole.text

    unknown = await client.post(
        f"/api/v1/calls/{uuid7()}/end", headers=_auth(rbac_world.admin_token)
    )
    assert unknown.status_code == 404, unknown.text

    # Published → visible to the non-owner supervisor now (no longer 404), but
    # VR2-59: before anyone has intervened, only the owner may end it.
    published = await client.post(
        f"/api/v1/calls/{call_id}/publish", headers=_auth(rbac_world.admin_token)
    )
    assert published.status_code == 200, published.text
    denied = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.supervisor_token)
    )
    assert denied.status_code == 409, denied.text
    assert "owner" in denied.json()["message"]

    # The owner may still end their own published call; the audit row carries the owner id.
    ended = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token)
    )
    assert ended.status_code == 200, ended.text
    audit = (
        (
            await admin_session.execute(
                select(AuditLog).where(
                    AuditLog.event_type == "call.end", AuditLog.resource_id == str(call_id)
                )
            )
        )
        .scalars()
        .one()
    )
    assert audit.detail == {"owner_id": str(rbac_world.admin_id), "phase": "live"}


@pytest.mark.asyncio
async def test_end_call_pre_answer_cancels_synchronously(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    call_stream_store: _MemCallStreamStore,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """End Call while still dialing: no worker session exists, so no call.ended
    arrives — the endpoint closes the call itself as CANCELED and resolves the
    form into EXCEPTION_REVIEW (parked for a human, never auto-redialed)."""
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    # Mirror the dispatcher: a form with a live call is IN_CALL.
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as s:
        form_row = (
            await s.execute(select(PatientForm).where(PatientForm.id == seeded_form_id))
        ).scalar_one()
        form_row.status = "in_call"

    resp = await client.post(f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text

    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.current_status == "canceled"
    assert row.end_requested_by_id == rbac_world.admin_id
    assert row.ended_at is not None
    assert room_name_for_call(rbac_world.tenant_id, call_id) in fake_livekit.deleted

    form = (
        await admin_session.execute(select(PatientForm).where(PatientForm.id == seeded_form_id))
    ).scalar_one()
    assert form.status == "exception_review"  # parked for a human; NOT re-queued

    audit = (
        (
            await admin_session.execute(
                select(AuditLog).where(
                    AuditLog.event_type == "call.end", AuditLog.resource_id == str(call_id)
                )
            )
        )
        .scalars()
        .one()
    )
    assert audit.detail["phase"] == "pre_answer"

    # The endpoint (not the absent worker) announces the cancel on the per-call
    # stream, so a tailing supervisor learns it. Asserted via the delete-surviving log.
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    assert (room, "canceled") in call_stream_store.status_log


@pytest.mark.asyncio
async def test_end_call_live_stamps_intent_and_defers_to_worker(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """End Call on an answered call: intent is stamped durably (so the sweeper
    closes as CANCELED, not FAILED, if call.ended never lands), but the status
    write is left to the worker-event consumer."""
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )
    resp = await client.post(f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text

    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.current_status == "active"  # the worker's call.ended owns closeout
    assert row.end_requested_by_id == rbac_world.admin_id
    assert room_name_for_call(rbac_world.tenant_id, call_id) in fake_livekit.deleted


@pytest.mark.asyncio
async def test_end_call_terminal_is_idempotent_noop(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="completed",
    )

    resp = await client.post(f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text

    # Already closed out: no room teardown, no audit row.
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    assert room not in fake_livekit.deleted
    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.end", AuditLog.resource_id == str(call_id)
            )
        )
    ).scalars()
    assert list(rows) == []


# ---------------------------------------------------------------------------
# ?intervene=true — calls:intervene gate + the single-intervener lock
# ---------------------------------------------------------------------------


async def _seed_published_active_call(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    form_id: UUID,
) -> UUID:
    """Admin-owned, published, live call — the canonical intervene-test setup."""
    return await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
        published=True,
    )


@pytest.mark.asyncio
async def test_end_call_locked_to_active_intervener(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    fake_livekit: FakeLiveKit,
) -> None:
    """While a takeover is live (claim inside the connect grace), only the
    intervener may end the call; the intervener's own end goes through."""
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)
    claim = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert claim.status_code == 200, claim.text

    denied = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.supervisor_token)
    )
    assert denied.status_code == 409, denied.text
    assert "intervening supervisor" in denied.json()["message"]

    allowed = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token)
    )
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_end_call_allowed_when_intervener_lock_is_stale(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    fake_livekit: FakeLiveKit,
) -> None:
    """A crashed intervener (claim past the grace, holder gone from the room)
    must not lock the call forever — a non-holder's end goes through."""
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)
    claim = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert claim.status_code == 200, claim.text

    # Age the claim past the grace window and drop the holder from the room: stale lock.
    await admin_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(intervener_claimed_at=text("now() - interval '5 minutes'"))
    )
    await admin_session.commit()
    room_name = room_name_for_call(rbac_world.tenant_id, call_id)
    fake_livekit.participants[room_name] = []

    ended = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.supervisor_token)
    )
    assert ended.status_code == 200, ended.text


async def _intervention_events(session: AsyncSession, call_id: UUID) -> list[InterventionEvent]:
    result = await session.execute(
        select(InterventionEvent).where(InterventionEvent.call_id == call_id)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_intervene_token_requires_calls_intervene(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)

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
            AuditLog.resource_id == f"/api/v1/calls/{call_id}/join-token",
            AuditLog.permission_key == "calls:intervene",
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_owner_without_calls_intervene_can_still_intervene(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The listener role holds only calls:read — but owning the call is enough on
    its own, per the confirmed owner-OR-permission rule (applies to Intervene too,
    not just the new Coaching feature)."""
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.listener_id,
        status="active",
        published=False,
    )

    claim = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.listener_token),
    )

    assert claim.status_code == 200, claim.text
    result = await admin_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == "authz.allow",
            AuditLog.resource_id == f"/api/v1/calls/{call_id}/join-token",
            AuditLog.permission_key == "calls:intervene",
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].detail.get("granted_via") == "owner"  # not "permission" — it never held it


@pytest.mark.asyncio
async def test_intervene_on_ownerless_call_requires_permission_too(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # An ownerless (dispatcher-created) call is watchable tenant-wide, but
    # intervening still needs calls:intervene.
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=None,
        status="active",
    )
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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Visibility beats capability: holding calls:intervene must not turn a
    # private call's 404 into a 403 (no enumeration).
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="active",
    )
    denied = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert denied.status_code == 404, denied.text


@pytest.mark.asyncio
async def test_intervene_claims_lock_and_writes_intervention_event(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)

    joined = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert joined.status_code == 200, joined.text

    call = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert call.intervener_user_id == rbac_world.supervisor_id
    assert call.intervener_claimed_at is not None

    events = await _intervention_events(admin_session, call_id)
    assert len(events) == 1
    assert events[0].type == "takeover"
    assert events[0].supervisor_id == rbac_world.supervisor_id

    # Token carries the supervisor's email + intervener mode; TTL capped at the grace.
    minted = fake_livekit.minted[-1]
    assert minted.can_publish is True
    assert minted.name == "supervisor@test.example"
    assert minted.attributes == {"vera.mode": "intervener"}
    assert minted.ttl == _INTERVENE_CONNECT_GRACE

    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.intervene.join",
                AuditLog.resource_id == str(call_id),
            )
        )
    ).scalars()
    assert len(list(rows)) == 1


@pytest.mark.asyncio
async def test_second_intervener_conflicts_within_grace(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)

    first = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert first.status_code == 200, first.text

    # A fresh claim is inside the connect-grace window, so the second caller is
    # refused with no staleness probe (participants is empty — a probe would allow a steal).
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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)
    first = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert first.status_code == 200, first.text

    # Age the claim past the grace window and drop the holder from the room: stale lock.
    await admin_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(intervener_claimed_at=text("now() - interval '5 minutes'"))
    )
    await admin_session.commit()
    room_name = room_name_for_call(rbac_world.tenant_id, call_id)
    fake_livekit.participants[room_name] = []

    stolen = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.admin_token),
    )
    assert stolen.status_code == 200, stolen.text

    call = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert call.intervener_user_id == rbac_world.admin_id

    # Both claims recorded; the steal's join audit names the released holder.
    assert len(await _intervention_events(admin_session, call_id)) == 2
    rows = (
        await admin_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "call.intervene.join",
                AuditLog.resource_id == str(call_id),
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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)
    first = await client.get(
        f"/api/v1/calls/{call_id}/join-token?intervene=true",
        headers=_auth(rbac_world.supervisor_token),
    )
    assert first.status_code == 200, first.text

    await admin_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(intervener_claimed_at=text("now() - interval '5 minutes'"))
    )
    await admin_session.commit()
    room_name = room_name_for_call(rbac_world.tenant_id, call_id)
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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)

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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
        status="completed",
        published=True,
    )
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
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    fake_livekit: FakeLiveKit,
) -> None:
    call_id = await _seed_published_active_call(admin_sessionmaker, rbac_world, seeded_form_id)

    watched = await client.get(
        f"/api/v1/calls/{call_id}/join-token", headers=_auth(rbac_world.supervisor_token)
    )
    assert watched.status_code == 200, watched.text

    minted = fake_livekit.minted[-1]
    assert minted.can_publish is False
    assert minted.name == "supervisor@test.example"
    assert minted.attributes == {"vera.mode": "listener"}
