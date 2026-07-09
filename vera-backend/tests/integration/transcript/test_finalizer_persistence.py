"""Drain → insert → redeliver → no duplicates (ON CONFLICT path, real Postgres).

Skips cleanly when local Postgres isn't up (run `just up && just migrate`).
"""

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.worker_events import WorkerEventConsumer
from vera_core.db import tenant_session, uuid7
from vera_core.events import CallEndedEvent
from vera_core.models import Call, PatientForm, Tenant, Transcript
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import InsuranceType
from vera_core.observability.correlation import room_name_for_call
from vera_core.transcript import ROLE_AGENT, ROLE_USER, InMemoryTranscriptStore, TranscriptService

pytestmark = pytest.mark.asyncio

_INSURANCE_TYPE = InsuranceType.INFERTILITY_TREATMENT.value


@pytest.fixture
async def db_sessionmaker(database_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Superuser (BYPASSRLS) sessionmaker.

    `database_url` (from tests/integration/conftest.py) already skips the test
    if Postgres is unreachable, so no extra guard is needed here.
    """
    engine = create_async_engine(database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def call_fixture(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[UUID, UUID, str, async_sessionmaker[AsyncSession]]]:
    """Create tenant + call rows. Yields (tenant_id, call_id, room_name, sessionmaker).

    Teardown removes all created rows so tests leave no residue.
    """
    tenant_id = uuid7()
    call_id = uuid7()
    patient_form_id = uuid7()
    room = room_name_for_call(tenant_id, call_id)

    schema_version_id: UUID
    form_schema_id: UUID
    created_schema = False

    async with db_sessionmaker() as session, session.begin():
        session.add(
            Tenant(id=tenant_id, slug=str(tenant_id), name="finalizer-test-tenant", status="active")
        )
        await session.flush()

        # Find or create the shared schema (seeded in CI; may already exist on dev DBs).
        schema = (
            await session.execute(
                select(FormSchema).where(FormSchema.insurance_type == _INSURANCE_TYPE)
            )
        ).scalar_one_or_none()
        if schema is None:
            created_schema = True
            schema = FormSchema(
                id=uuid7(), insurance_type=_INSURANCE_TYPE, name="Finalizer Test Schema"
            )
            session.add(schema)
            await session.flush()
            sv = SchemaVersion(id=uuid7(), schema_id=schema.id, version=1, schema_json={})
            session.add(sv)
            await session.flush()
            schema_version_id = sv.id
            form_schema_id = schema.id
        else:
            form_schema_id = schema.id
            schema_version_id = (
                await session.execute(
                    select(SchemaVersion.id)
                    .where(SchemaVersion.schema_id == schema.id)
                    .order_by(SchemaVersion.version.desc())
                    .limit(1)
                )
            ).scalar_one()

        session.add(
            PatientForm(
                id=patient_form_id,
                tenant_id=tenant_id,
                schema_version_id=schema_version_id,
                patient_name="Test Patient",
            )
        )
        await session.flush()
        session.add(Call(id=call_id, tenant_id=tenant_id, form_id=patient_form_id))

    yield tenant_id, call_id, room, db_sessionmaker

    # Teardown — inner-to-outer FK order.
    async with db_sessionmaker() as session, session.begin():
        await session.execute(
            text("DELETE FROM transcript WHERE call_id = :cid").bindparams(cid=call_id)
        )
        await session.execute(
            text(
                "DELETE FROM call_event WHERE call_id IN (SELECT id FROM call WHERE form_id = :fid)"
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
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(sid=schema_version_id)
            )
            await session.execute(
                text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=form_schema_id)
            )
        # Remove any audit artefacts that reference this tenant before dropping it.
        for table in ("audit_log", "auth_audit_log"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
        await session.execute(text("DELETE FROM tenant WHERE id = :tid").bindparams(tid=tenant_id))


async def test_finalizer_idempotent_insert(
    call_fixture: tuple[UUID, UUID, str, async_sessionmaker[AsyncSession]],
) -> None:
    """Calling the handler twice must produce exactly 2 rows (not 4)."""
    tenant_id, call_id, room, sm = call_fixture

    store = InMemoryTranscriptStore()
    svc = TranscriptService(store)
    await svc.publish_turn(room, ROLE_USER, "[[NAME_1]] speaking", ts=1_700_000_000_000)
    await svc.publish_turn(room, ROLE_AGENT, "hello [[NAME_1]]", ts=1_700_000_001_000)
    await svc.end(room)

    # Use a bare object for `livekit` — the call.ended handler never touches it.
    consumer = WorkerEventConsumer(
        object(),  # type: ignore[arg-type]
        livekit=object(),  # type: ignore[arg-type]
        sessionmaker=sm,
        transcripts=svc,
    )

    event = CallEndedEvent(room_name=room, ts=1_700_000_005_000)

    # First delivery — inserts 2 rows.
    await consumer._handle_call_ended(event)

    # Second delivery (redelivery after a crash) — ON CONFLICT DO NOTHING, still 2 rows.
    await consumer._handle_call_ended(event)

    async with tenant_session(sm, tenant_id) as session:
        rows = (
            (await session.execute(select(Transcript).where(Transcript.call_id == call_id)))
            .scalars()
            .all()
        )

    assert len(rows) == 2, f"expected 2 transcript rows, got {len(rows)}"
    assert {r.seq for r in rows} == {0, 1}
