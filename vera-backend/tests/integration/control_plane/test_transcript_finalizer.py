"""Integration test: `finalize_transcript` against a live RLS-enforcing Postgres
(via `authz_app`, same as test_calls.py) and the app's real CallStreamService.
The real-DB counterpart of tests/unit/control_plane/test_transcript_finalizer.py,
which fakes the DB seam — this proves the actual `Transcript` insert, RLS tenant
pinning, and the `UNIQUE(call_id, seq)` / ON CONFLICT DO NOTHING dedup.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.transcript_finalizer import finalize_transcript
from tests.integration.control_plane.conftest import RBACWorld, seed_call
from vera_core.call_stream import CallStreamService
from vera_core.db import uuid7
from vera_core.db.rls import tenant_session
from vera_core.models import PatientForm
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import CallStatus, InsuranceType
from vera_core.models.transcript import Transcript
from vera_core.observability.correlation import RoomRef, room_name_for_call


@pytest.fixture
async def seeded_form_id(database_url: str, rbac_world: RBACWorld) -> AsyncGenerator[UUID]:
    """Trimmed copy of test_calls.py's fixture of the same name (module-local
    fixtures aren't shared across test files here): a PatientForm attached to a
    find-or-create INFERTILITY_TREATMENT FormSchema/SchemaVersion chain, torn down
    afterwards so rbac_world can delete the tenant without a FK violation."""
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
            if schema is None:
                schema = FormSchema(
                    id=uuid7(), insurance_type=InsuranceType.INFERTILITY_TREATMENT.value, name="x"
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
            session.add(
                PatientForm(
                    id=patient_form_id,
                    tenant_id=rbac_world.tenant_id,
                    schema_version_id=schema_version_id,
                    patient_name="Test Patient",
                )
            )
        yield patient_form_id
    finally:
        # Remove call rows referencing this form (and their events) before
        # rbac_world deletes the tenant. The schema chain is left in place —
        # it may be the shared seeded one (test_calls.py's fixture owns tearing
        # down one it created itself).
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
        await engine.dispose()


@pytest.fixture
async def call_id(authz_app: FastAPI, rbac_world: RBACWorld, seeded_form_id: UUID) -> UUID:
    return await seed_call(
        authz_app.state.sessionmaker,
        rbac_world.tenant_id,
        seeded_form_id,
        status=CallStatus.ACTIVE.value,
    )


async def _rows(authz_app: FastAPI, tenant_id: UUID, call_id: UUID) -> list[Transcript]:
    async with tenant_session(authz_app.state.sessionmaker, tenant_id) as session:
        result = await session.execute(
            select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.seq)
        )
        return list(result.scalars().all())


async def test_finalize_persists_rows_under_the_correct_tenant_and_clears_stream(
    authz_app: FastAPI,
    rbac_world: RBACWorld,
    call_stream_service: CallStreamService,
    call_id: UUID,
) -> None:
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    await call_stream_service.publish_turn(room, "user", "hello", ts=1000)
    await call_stream_service.publish_turn(room, "agent", "hi there", ts=2000)
    ref = RoomRef(tenant_id=rbac_world.tenant_id, call_id=call_id)

    count = await finalize_transcript(authz_app.state.sessionmaker, call_stream_service, ref, room)

    assert count == 2
    rows = await _rows(authz_app, rbac_world.tenant_id, call_id)
    assert [r.seq for r in rows] == [0, 1]
    assert [r.role for r in rows] == ["user", "agent"]
    assert [r.source for r in rows] == ["rep", "bot"]
    assert [r.message for r in rows] == ["hello", "hi there"]
    assert all(r.tenant_id == rbac_world.tenant_id for r in rows)
    assert all(r.spoke_at is not None for r in rows)

    assert await call_stream_service.read_all(room) == []  # stream cleared


async def test_finalize_on_a_redelivered_call_ended_does_not_duplicate_rows(
    authz_app: FastAPI,
    rbac_world: RBACWorld,
    call_stream_service: CallStreamService,
    call_id: UUID,
) -> None:
    """The UNIQUE(call_id, seq) constraint + ON CONFLICT DO NOTHING must hold even
    when the same turns land on the stream a second time (e.g. an at-least-once
    redelivery racing the first clear) — the DB must never end up with duplicates."""
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    ref = RoomRef(tenant_id=rbac_world.tenant_id, call_id=call_id)
    await call_stream_service.publish_turn(room, "user", "hello", ts=1000)

    first = await finalize_transcript(authz_app.state.sessionmaker, call_stream_service, ref, room)
    assert first == 1

    # Simulate the same turn reappearing on the stream (same role/text/ts -> the
    # same seq=0 when re-mapped) and finalize running again.
    await call_stream_service.publish_turn(room, "user", "hello", ts=1000)
    second = await finalize_transcript(authz_app.state.sessionmaker, call_stream_service, ref, room)
    assert second == 1  # rows produced by this pass; ON CONFLICT absorbs the dupe

    rows = await _rows(authz_app, rbac_world.tenant_id, call_id)
    assert len(rows) == 1  # NOT 2 — the constraint held


async def test_finalize_on_an_empty_stream_writes_nothing_and_does_not_error(
    authz_app: FastAPI,
    rbac_world: RBACWorld,
    call_stream_service: CallStreamService,
    call_id: UUID,
) -> None:
    """A room whose stream never existed (e.g. a call that ended before any turn
    was published) must finalize cleanly with zero rows."""
    room = room_name_for_call(rbac_world.tenant_id, call_id)
    ref = RoomRef(tenant_id=rbac_world.tenant_id, call_id=call_id)

    count = await finalize_transcript(authz_app.state.sessionmaker, call_stream_service, ref, room)

    assert count == 0
    assert await _rows(authz_app, rbac_world.tenant_id, call_id) == []
