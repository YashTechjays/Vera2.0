"""Integration coverage for `add_agent_context_metadata`'s DB glue — the SchemaVersion lookup,
the `is_v2` gate, and the `FieldAnswer.is_current` filter — against real Postgres and the real
published ibv v2 schema. The unit tests cover the pure `build_agent_context`; this proves the
query wiring resolves values from the actual `field_answer` rows. Skips without Postgres.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.seed import _seed_form_schemas
from vera_core.models import FieldAnswer, FormSchema, PatientForm, SchemaVersion
from vera_core.models.enums import AnswerSource, FormStatus, InsuranceType, VersionStatus
from vera_core.services.ivr_selection import add_agent_context_metadata

from .conftest import RBACWorld

# v2 field paths are root-anchored (`sections.…`) — byte-identical to the schema's system_fields.
_MEMBER = "sections.insurance_information.policy_number"  # member_id handle
_NAME = "sections.patient_information.patient_name"
_DOB = "sections.patient_information.patient_dob"


@pytest.fixture
async def schema_version_id(admin_sessionmaker: async_sessionmaker[AsyncSession]) -> UUID:
    async with admin_sessionmaker() as s, s.begin():
        await _seed_form_schemas(s)
    async with admin_sessionmaker() as s:
        return (
            await s.execute(
                select(SchemaVersion.id)
                .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                .where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value,
                    SchemaVersion.status == VersionStatus.PUBLISHED.value,
                )
            )
        ).scalar_one()


@pytest.fixture
async def cleanup_forms(
    admin_sessionmaker: async_sessionmaker[AsyncSession], rbac_world: RBACWorld
) -> AsyncGenerator[None]:
    yield
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "DELETE FROM field_answer WHERE form_id IN "
                "(SELECT id FROM patient_form WHERE tenant_id = :t)"
            ).bindparams(t=rbac_world.tenant_id)
        )
        await s.execute(
            text("DELETE FROM patient_form WHERE tenant_id = :t").bindparams(t=rbac_world.tenant_id)
        )


async def _seed_form(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    schema_version_id: UUID,
    answers: list[tuple[str, str, bool]],
) -> PatientForm:
    """Seed a form + `(field_path, value, is_current)` field answers; return the detached form."""
    async with sm() as s, s.begin():
        form = PatientForm(
            tenant_id=tenant_id,
            schema_version_id=schema_version_id,
            status=FormStatus.READY_FOR_PROCESSING.value,
            intake_payload={},
            completion_pct=0,
            retry_count=0,
        )
        s.add(form)
        await s.flush()
        for path, value, is_current in answers:
            s.add(
                FieldAnswer(
                    tenant_id=tenant_id,
                    form_id=form.id,
                    field_path=path,
                    value={"value": value},
                    source=AnswerSource.INTAKE.value,
                    is_current=is_current,
                )
            )
        s.expunge(form)  # keep form.id / form.schema_version_id usable after the session closes
        return form


async def test_add_agent_context_metadata_resolves_current_field_answers(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    form = await _seed_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        answers=[
            (_MEMBER, "POL-661522", True),
            (_NAME, "jane roe", True),
            (_DOB, "1990-03-07", False),  # superseded — must be ignored
            (_DOB, "1990-03-08", True),  # current — wins
        ],
    )

    metadata: dict[str, object] = {}
    async with admin_sessionmaker() as s:
        await add_agent_context_metadata(s, form, metadata)

    ctx = metadata["agent_context"]
    assert isinstance(ctx, dict)
    assert ctx["member_id"] == "POL-661522"
    assert ctx["patient_name"] == "jane roe"
    assert ctx["patient_dob"] == "03/08/1990"  # the CURRENT answer, date-normalized for speech


async def test_add_agent_context_metadata_attaches_nothing_without_answers(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    schema_version_id: UUID,
    cleanup_forms: None,
) -> None:
    form = await _seed_form(
        admin_sessionmaker,
        tenant_id=rbac_world.tenant_id,
        schema_version_id=schema_version_id,
        answers=[],
    )
    metadata: dict[str, object] = {}
    async with admin_sessionmaker() as s:
        await add_agent_context_metadata(s, form, metadata)
    assert "agent_context" not in metadata
