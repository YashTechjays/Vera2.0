"""Regression: concurrent `try_dispatch` passes must not overshoot the tenant's
concurrency cap (`max_agents_per_va`).

Found by load testing: the cap check is read-then-act — under READ COMMITTED,
concurrent passes don't see each other's uncommitted dispatches, so each reads
the same active-count and each fills the "free" slots. FOR UPDATE SKIP LOCKED
prevents dispatching the *same form* twice, but not blowing the cap (observed
16 passes x cap 10 -> 160 live calls). Both production triggers can overlap:
bulk enqueues, or several call-end callbacks landing together.

Runs against live RLS-enforcing Postgres — the race only exists across real
concurrent transactions.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.db import tenant_session, uuid7
from vera_core.models import Call, CallEvent, PatientForm, Tenant
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import FormStatus, InsuranceType
from vera_core.services.queue_dispatcher import try_dispatch

_MAX_AGENTS = 3
_QUEUED_FORMS = 24
_CONCURRENT_PASSES = 8


class _NullLiveKit:
    """Duck-typed LiveKitGateway stand-in — room provisioning is not under test."""

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        return None


@pytest.fixture
async def queued_tenant(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[UUID]:
    """A tenant with a small cap and a deep queue, superuser-created.

    `form_schema.insurance_type` is a globally UNIQUE catalog key and CI seeds
    the INFERTILITY_TREATMENT schema before pytest, so the schema chain is
    find-or-create; teardown drops only what this fixture created."""
    tenant_id = uuid7()
    async with admin_sessionmaker() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"qdr-{tenant_id.hex[:8]}",
                name=f"Queue race {tenant_id.hex[:8]}",
                status="active",
                max_agents_per_va=_MAX_AGENTS,
            )
        )
        await session.flush()
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
                name="Queue Race Test Schema",
            )
            session.add(schema)
            await session.flush()
            schema_version_id = uuid7()
            session.add(
                SchemaVersion(id=schema_version_id, schema_id=schema.id, version=1, schema_json={})
            )
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
        for _ in range(_QUEUED_FORMS):
            session.add(
                PatientForm(
                    tenant_id=tenant_id,
                    schema_version_id=schema_version_id,
                    status=FormStatus.IN_QUEUE.value,
                    enqueued_at=func.now(),
                )
            )

    yield tenant_id

    async with admin_sessionmaker() as session, session.begin():
        await session.execute(delete(CallEvent).where(CallEvent.tenant_id == tenant_id))
        await session.execute(delete(Call).where(Call.tenant_id == tenant_id))
        await session.execute(delete(PatientForm).where(PatientForm.tenant_id == tenant_id))
        if created_schema:
            await session.execute(
                delete(SchemaVersion).where(SchemaVersion.id == schema_version_id)
            )
            await session.execute(delete(FormSchema).where(FormSchema.id == form_schema_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def test_concurrent_dispatch_passes_respect_concurrency_cap(
    queued_tenant: UUID,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async def one_pass() -> int:
        async with tenant_session(rls_sessionmaker, queued_tenant) as session:
            return await try_dispatch(session, queued_tenant, _NullLiveKit())

    dispatched = await asyncio.gather(*(one_pass() for _ in range(_CONCURRENT_PASSES)))

    assert sum(dispatched) == _MAX_AGENTS, (
        f"concurrency cap overshot: {sum(dispatched)} calls initiated across "
        f"{_CONCURRENT_PASSES} concurrent passes, cap is {_MAX_AGENTS} ({dispatched})"
    )
    async with admin_sessionmaker() as session:
        calls = (
            await session.execute(
                select(func.count()).select_from(Call).where(Call.tenant_id == queued_tenant)
            )
        ).scalar_one()
        in_call = (
            await session.execute(
                select(func.count())
                .select_from(PatientForm)
                .where(
                    PatientForm.tenant_id == queued_tenant,
                    PatientForm.status == FormStatus.IN_CALL.value,
                )
            )
        ).scalar_one()
    assert calls == _MAX_AGENTS
    assert in_call == _MAX_AGENTS
