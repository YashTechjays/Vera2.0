"""In-app patient-form creation (session-auth): `GET /api/v1/patient-forms/schemas`
(the selectable form families) and `POST /api/v1/patient-forms:create` (bind to the
family's published version, persist form + INTAKE answers). Skips without Postgres."""

from collections.abc import AsyncGenerator
from datetime import date
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.dispatch import drain_pending
from scripts.seed import _seed_form_schemas
from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import tenant_session
from vera_core.models import FieldAnswer, FormSchema, PatientForm, SchemaVersion
from vera_core.models.enums import InsuranceType, VersionStatus

INTAKE_PAYLOAD = {
    "patient_information": {
        "patient_name": "Jane Doe",
        "patient_dob": "1990-04-12",
        "patient_gender": "Female",
    },
    "appointment_information": {"appointment_date": "2026-08-03"},
    "insurance_information": {"policy_number": "POL-550411"},
    "insurance_reference_information": {
        "insurance_provider_name": "Demo Health Plan",
        "insurance_phone_number": "+1 555 0100",
    },
    "verification_information": {"verified_by": "Dr. Reyes"},
    "hospital_information": {
        "hospital_name": "Demo Health Partners",
        "hospital_address": "123 Demo St, Austin, TX",
        "tax_id": "987654313",
        "npi": "1234567893",
    },
    "provider_reference_information": {
        "provider_name": "Dr. Jane Smith",
        "npi": "1982736450",
    },
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def ibv_schema(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID]:
    """Seed the catalog (idempotent) and return (schema_id, published_version_id)
    for the infertility_treatment family."""
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
    async with admin_sessionmaker() as session:
        row = (
            await session.execute(
                select(SchemaVersion.schema_id, SchemaVersion.id)
                .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                .where(
                    FormSchema.insurance_type == InsuranceType.INFERTILITY_TREATMENT.value,
                    SchemaVersion.status == VersionStatus.PUBLISHED.value,
                )
            )
        ).one()
    return row[0], row[1]


@pytest.fixture
async def cleanup_forms(
    admin_sessionmaker: async_sessionmaker[AsyncSession], rbac_world: RBACWorld
) -> AsyncGenerator[None]:
    yield
    await drain_pending()
    async with admin_sessionmaker() as session, session.begin():
        # field_answer cascades on the form delete.
        await session.execute(
            text("DELETE FROM patient_form WHERE tenant_id IN (:a, :b)").bindparams(
                a=rbac_world.tenant_id, b=rbac_world.other_tenant_id
            )
        )


async def test_list_schemas_returns_published_families(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    ibv_schema: tuple[UUID, UUID],
) -> None:
    schema_id, version_id = ibv_schema
    resp = await client.get("/api/v1/patient-forms/schemas", headers=_auth(rbac_world.admin_token))
    # Also proves route order: this must not be swallowed by /patient-forms/{form_id}
    # (which would 422 on a non-UUID path segment).
    assert resp.status_code == 200, resp.text
    options = resp.json()["data"]
    by_id = {o["schema_id"]: o for o in options}
    assert str(schema_id) in by_id
    option = by_id[str(schema_id)]
    assert option["published_version_id"] == str(version_id)
    assert option["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value
    assert option["name"]
    assert option["published_version"] >= 1


async def test_list_schemas_requires_forms_read(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    ibv_schema: tuple[UUID, UUID],
) -> None:
    resp = await client.get("/api/v1/patient-forms/schemas", headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403, resp.text


async def test_create_binds_published_version_and_persists(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    schema_id, published_version_id = ibv_schema
    resp = await client.post(
        "/api/v1/patient-forms:create",
        json={"schema_id": str(schema_id), "intake_payload": INTAKE_PAYLOAD},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "ready_for_processing"
    # The server resolved the family's published version — the client never sent one.
    assert data["schema_version_id"] == str(published_version_id)
    assert data["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value
    form_id = UUID(data["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.tenant_id) as session:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one()
        assert form.tenant_id == rbac_world.tenant_id  # tenant from session, not input
        assert form.status == "ready_for_processing"
        assert form.patient_name == "jane doe"  # promoted + normalized
        assert form.patient_dob == date(1990, 4, 12)
        assert form.member_id == "POL-550411"
        assert form.insurance_provider == "Demo Health Plan"
        assert form.intake_payload == INTAKE_PAYLOAD

        answers = (
            (await session.execute(select(FieldAnswer).where(FieldAnswer.form_id == form_id)))
            .scalars()
            .all()
        )
        assert len(answers) == 14  # one INTAKE answer per provided leaf
        assert all(a.source == "intake" and a.is_current and a.call_id is None for a in answers)
        assert "sections.patient_information.patient_name" in {a.field_path for a in answers}


async def test_create_missing_required_returns_422_with_paths_no_phi(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    schema_id, _ = ibv_schema
    resp = await client.post(
        "/api/v1/patient-forms:create",
        json={
            "schema_id": str(schema_id),
            "intake_payload": {"patient_information": {"patient_name": "Secret Patient"}},
        },
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 422, resp.text
    assert "sections.patient_information.patient_dob" in resp.json()["data"]["fields"]
    assert "Secret Patient" not in resp.text  # never echo a PHI value


async def test_create_unknown_schema_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    ibv_schema: tuple[UUID, UUID],
) -> None:
    from vera_core.db import uuid7

    resp = await client.post(
        "/api/v1/patient-forms:create",
        json={"schema_id": str(uuid7()), "intake_payload": INTAKE_PAYLOAD},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 404, resp.text


@pytest.fixture
async def unpublished_schema_id(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[UUID]:
    """The disease_only family with its published version temporarily demoted to
    draft — restored afterwards (the seeded catalog is shared session state)."""
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
        schema_id, version_id = (
            await session.execute(
                select(SchemaVersion.schema_id, SchemaVersion.id)
                .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                .where(
                    FormSchema.insurance_type == InsuranceType.DISEASE_ONLY.value,
                    SchemaVersion.status == VersionStatus.PUBLISHED.value,
                )
            )
        ).one()
        await session.execute(
            update(SchemaVersion)
            .where(SchemaVersion.id == version_id)
            .values(status=VersionStatus.DRAFT.value)
        )
    yield schema_id
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            update(SchemaVersion)
            .where(SchemaVersion.id == version_id)
            .values(status=VersionStatus.PUBLISHED.value)
        )


async def test_create_without_published_version_returns_409(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    unpublished_schema_id: UUID,
) -> None:
    resp = await client.post(
        "/api/v1/patient-forms:create",
        json={"schema_id": str(unpublished_schema_id), "intake_payload": INTAKE_PAYLOAD},
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 409, resp.text


async def test_create_requires_forms_write(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    ibv_schema: tuple[UUID, UUID],
) -> None:
    schema_id, _ = ibv_schema
    resp = await client.post(
        "/api/v1/patient-forms:create",
        json={"schema_id": str(schema_id), "intake_payload": INTAKE_PAYLOAD},
        headers=_auth(rbac_world.norole_token),
    )
    assert resp.status_code == 403, resp.text


async def test_create_rls_isolation_other_tenant_cannot_see_row(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    schema_id, _ = ibv_schema
    resp = await client.post(
        "/api/v1/patient-forms:create",
        json={"schema_id": str(schema_id), "intake_payload": INTAKE_PAYLOAD},
        headers=_auth(rbac_world.admin_token),
    )
    form_id = UUID(resp.json()["data"]["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.other_tenant_id) as session:
        found = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one_or_none()
    assert found is None
