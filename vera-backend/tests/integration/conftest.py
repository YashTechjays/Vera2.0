"""Real-Postgres fixtures. These tests run when a database is reachable
(docker-compose locally, a service container in CI) and skip otherwise.

The compose/CI user is a superuser, which BYPASSES row-level security — so the
fixtures create a dedicated non-superuser role and a second engine connected as
it; that engine is what the RLS assertions use.
"""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vera_core.config import Settings
from vera_core.db import tenant_session, uuid7
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import AnswerSource, FormStatus, InsuranceType
from vera_core.models.field_answer import FieldAnswer, FieldEvaluation
from vera_core.models.patient_form import PatientForm
from vera_core.models.tenant import Tenant

RLS_ROLE = "vera_rls_test"
RLS_PASSWORD = "vera_rls_test"


def _database_url() -> str:
    return Settings(_env_file=None).database_url


async def _can_connect(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with asyncio.timeout(2):
            async with engine.connect():
                return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()
    if not asyncio.run(_can_connect(url)):
        pytest.skip("postgres not reachable — run `just up && just migrate`")
    return url


@pytest.fixture(scope="session")
def rls_database_url(database_url: str) -> str:
    """Create the non-superuser role + grants, return a URL connecting as it."""

    async def setup() -> None:
        engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            role_exists = await conn.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r").bindparams(r=RLS_ROLE)
            )
            if not role_exists:
                await conn.execute(text(f"CREATE ROLE {RLS_ROLE} LOGIN PASSWORD '{RLS_PASSWORD}'"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {RLS_ROLE}"))
            await conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public"
                    f" TO {RLS_ROLE}"
                )
            )
            # vera_rls_test stands in for the deployed app role, which the definer
            # functions now grant EXECUTE to explicitly (migration f066c667ddc1 revokes
            # the PUBLIC default). Mirror that grant so the RLS role can still invoke them.
            await conn.execute(
                text(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {RLS_ROLE}")
            )
        await engine.dispose()

    asyncio.run(setup())
    scheme, rest = database_url.split("://", 1)
    host_part = rest.split("@", 1)[1]
    return f"{scheme}://{RLS_ROLE}:{RLS_PASSWORD}@{host_part}"


@pytest.fixture
async def admin_engine(database_url: str) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def rls_engine(rls_database_url: str) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(rls_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def admin_sessionmaker(admin_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(admin_engine, expire_on_commit=False)


@pytest.fixture
async def rls_sessionmaker(rls_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(rls_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# seeded_form_with_answers — fixture for test_load_field_status
# ---------------------------------------------------------------------------

_FIELD_STATUS_SCHEMA: dict[str, object] = {
    "dsl_version": "2.1",
    "name": "FieldStatus Test Schema",
    "insurance_type": "disease_only",
    "sections": {
        "coverage": {
            "title": "Coverage",
            "role": "collect",
            "fields": {
                "a": {
                    "type": "text",
                    "title": "Field A",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What is A?"},
                },
                "b": {
                    "type": "text",
                    "title": "Field B",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "What is B?"},
                },
            },
        }
    },
    "tasks": [{"task_key": "main", "title": "Main Task", "sections": ["coverage"]}],
}


@dataclass
class _FieldStatusCtx:
    tenant_id: UUID
    form_id: UUID
    session: AsyncSession


@pytest.fixture
async def seeded_form_with_answers(
    database_url: str,
) -> AsyncGenerator[_FieldStatusCtx]:
    """Seed a PatientForm with two FieldAnswer rows and one FieldEvaluation.

    - cov.a: source=ai_call, confidence=55, is_current=True → FieldEvaluation(supported=False)
    - cov.b: source=human, is_current=True → no evaluation

    Uses the superuser engine (bypasses RLS) for inserts; yields a tenant-pinned
    session so load_field_status runs under the correct RLS context. Tears down in
    FK order.
    """
    tenant_id = uuid7()
    form_id = uuid7()
    schema_version_id = uuid7()

    engine = create_async_engine(database_url)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    created_schema = False
    schema_id: UUID

    async with sm() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=str(tenant_id),
                name="FieldStatus Test Tenant",
                status="active",
            )
        )
        # find-or-create to avoid UNIQUE collision on insurance_type
        existing_schema = (
            await session.execute(
                select(FormSchema).where(
                    FormSchema.insurance_type == InsuranceType.DISEASE_ONLY.value
                )
            )
        ).scalar_one_or_none()
        if existing_schema is None:
            new_schema = FormSchema(
                id=uuid7(),
                insurance_type=InsuranceType.DISEASE_ONLY.value,
                name="FieldStatus Test Schema",
            )
            session.add(new_schema)
            await session.flush()
            schema_id = new_schema.id
            created_schema = True
        else:
            schema_id = existing_schema.id

        session.add(
            SchemaVersion(
                id=schema_version_id,
                schema_id=schema_id,
                version=998,
                schema_json=_FIELD_STATUS_SCHEMA,
            )
        )
        await session.flush()

        session.add(
            PatientForm(
                id=form_id,
                tenant_id=tenant_id,
                schema_version_id=schema_version_id,
                patient_name="Test Patient",
                status=FormStatus.AI_PROCESSING.value,
            )
        )
        await session.flush()

        # ai_call answer at cov.a with confidence=55
        answer_a_id = uuid7()
        session.add(
            FieldAnswer(
                id=answer_a_id,
                tenant_id=tenant_id,
                form_id=form_id,
                field_path="cov.a",
                source=AnswerSource.AI_CALL.value,
                value={"value": "some-value"},
                confidence=55,
                is_current=True,
            )
        )
        # human answer at cov.b — no evaluation
        session.add(
            FieldAnswer(
                id=uuid7(),
                tenant_id=tenant_id,
                form_id=form_id,
                field_path="cov.b",
                source=AnswerSource.HUMAN.value,
                value={"value": "human-value"},
                confidence=None,
                is_current=True,
            )
        )
        # FieldEvaluation for the ai_call answer: supported=False
        # No flush needed before this — answer_a_id is a locally generated uuid7(),
        # not a DB-assigned key, so the FK can resolve at commit time.
        session.add(
            FieldEvaluation(
                id=uuid7(),
                tenant_id=tenant_id,
                answer_id=answer_a_id,
                supported=False,
            )
        )

    async with tenant_session(sm, tenant_id) as test_session:
        yield _FieldStatusCtx(
            tenant_id=tenant_id,
            form_id=form_id,
            session=test_session,
        )

    # teardown in FK order
    try:
        async with sm() as session, session.begin():
            await session.execute(
                text("DELETE FROM field_evaluation WHERE tenant_id = :tid").bindparams(
                    tid=tenant_id
                )
            )
            await session.execute(
                text("DELETE FROM field_answer WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE tenant_id = :tid").bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(sid=schema_version_id)
            )
            if created_schema:
                await session.execute(
                    text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=schema_id)
                )
            await session.execute(
                text("DELETE FROM tenant WHERE id = :tid").bindparams(tid=tenant_id)
            )
    finally:
        await engine.dispose()
