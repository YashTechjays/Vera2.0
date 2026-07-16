"""The IBV intake endpoint (`POST /api/v1/patient-forms`): inbound API-key auth,
client-supplied form/version, persisted PatientForm + INTAKE-source field_answer
rows, and tenant isolation. Skips without a reachable Postgres (see conftest)."""

from collections.abc import AsyncGenerator
from datetime import date
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth import api_key as apikey
from control_plane.dispatch import drain_pending
from scripts.seed import _seed_form_schemas
from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import tenant_session, uuid7
from vera_core.models import ApiKey, FieldAnswer, FormSchema, PatientForm, SchemaVersion
from vera_core.models.enums import InsuranceType, VersionStatus

INTAKE_PAYLOAD = {
    "patient_information": {
        "chart_number": "CH-10293",
        "patient_name": "Jane Doe",
        "patient_dob": "1990-04-12",
        "patient_gender": "Female",
    },
    "appointment_information": {"appointment_date": "2026-08-03"},
    "insurance_information": {"policy_number": "POL-550411"},
    "insurance_reference_information": {
        "insurance_provider_name": "Demo Health Plan",
        "insurance_phone_number": "+15550100",
    },
    "verification_information": {
        "verified_by": "Dr. Reyes",
        "callback_number": "+1 555 0199",
    },
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


@pytest.fixture
async def ibv_schema(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID]:
    """Seed the IBV schema (idempotent) and return (form_type_id, schema_version_id)
    for its published version."""
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
    # An in-flight detached dispatch task could insert a call row FK-referencing
    # a form mid-delete — let it finish first.
    await drain_pending()
    async with admin_sessionmaker() as session, session.begin():
        # field_answer cascades on the form delete.
        await session.execute(
            text("DELETE FROM patient_form WHERE tenant_id IN (:a, :b)").bindparams(
                a=rbac_world.tenant_id, b=rbac_world.other_tenant_id
            )
        )


async def _issue_key(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    scope: str = "intake:write",
) -> str:
    """Insert an ApiKey row directly and return its plaintext `vk_...` token."""
    key_id = uuid7()
    salt = apikey.new_salt()
    secret = apikey.new_secret()
    async with admin_sessionmaker() as session, session.begin():
        session.add(
            ApiKey(
                id=key_id,
                tenant_id=tenant_id,
                # Unique per call: rbac_world is session-scoped, so a fixed name would
                # violate uq_api_key_tenant_name_active (one active key per tenant+name)
                # the second time a test issues a key for the shared tenant.
                name=f"sheet-{key_id}",
                salt=salt,
                key_hash=apikey.hash_secret(salt, secret),
                scope=scope,
                expires_at=None,
                revoked=False,
            )
        )
    return apikey.format_token(tenant_id, key_id, secret)


async def test_upload_creates_form_and_intake_answers(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": INTAKE_PAYLOAD,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "ready_for_processing"
    assert data["insurance_type"] == InsuranceType.INFERTILITY_TREATMENT.value
    assert data["schema_version_id"] == str(version_id)
    form_id = UUID(data["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.tenant_id) as session:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one()
        assert form.status == "ready_for_processing"
        assert form.patient_name == "jane doe"  # promoted + normalized
        assert form.patient_dob == date(1990, 4, 12)
        assert form.intake_payload == INTAKE_PAYLOAD

        answers = (
            (await session.execute(select(FieldAnswer).where(FieldAnswer.form_id == form_id)))
            .scalars()
            .all()
        )
        # v2 documents record root-anchored paths (`sections.…` = field_answer.field_path).
        assert {a.field_path for a in answers} == {
            "sections.patient_information.chart_number",
            "sections.patient_information.patient_name",
            "sections.patient_information.patient_dob",
            "sections.patient_information.patient_gender",
            "sections.appointment_information.appointment_date",
            "sections.insurance_information.policy_number",
            "sections.insurance_reference_information.insurance_provider_name",
            "sections.insurance_reference_information.insurance_phone_number",
            "sections.verification_information.verified_by",
            "sections.verification_information.callback_number",
            "sections.hospital_information.hospital_name",
            "sections.hospital_information.hospital_address",
            "sections.hospital_information.tax_id",
            "sections.hospital_information.npi",
            "sections.provider_reference_information.provider_name",
            "sections.provider_reference_information.npi",
        }
        assert all(a.source == "intake" and a.is_current and a.call_id is None for a in answers)


async def test_upload_promotes_worklist_columns(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    payload = {
        **INTAKE_PAYLOAD,
        "appointment_information": {
            **INTAKE_PAYLOAD["appointment_information"],
            "appointment_type": "New Patient",
        },
        "insurance_reference_information": {
            "insurance_provider_name": "Blue Cross",
            "insurance_phone_number": "+15550100",
        },
    }
    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": payload,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    form_id = UUID(resp.json()["data"]["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.tenant_id) as session:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one()
        assert form.appointment_type == "New Patient"
        assert form.member_id == "POL-550411"
        assert form.insurance_provider == "Blue Cross"
        assert form.insurance_provider_phone_number == "+15550100"


async def test_missing_required_returns_422_with_paths_no_phi(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": {"patient_information": {"patient_name": "Secret Patient"}},
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    # v2 required-at-intake = the schema's `system_fields` targets without a
    # declared default (appointment_type keeps its "N/A" default, so it stays the
    # one exempt target), reported root-anchored, across every section — not just
    # `patient_information`.
    assert set(body["data"]["fields"]) == {
        "sections.patient_information.chart_number",
        "sections.patient_information.patient_gender",
        "sections.patient_information.patient_dob",
        "sections.appointment_information.appointment_date",
        "sections.insurance_information.policy_number",
        "sections.insurance_reference_information.insurance_provider_name",
        "sections.insurance_reference_information.insurance_phone_number",
        "sections.verification_information.verified_by",
        "sections.verification_information.callback_number",
        "sections.hospital_information.hospital_name",
        "sections.hospital_information.hospital_address",
        "sections.hospital_information.tax_id",
        "sections.hospital_information.npi",
        "sections.provider_reference_information.provider_name",
        "sections.provider_reference_information.npi",
    }
    assert "Secret Patient" not in resp.text  # never echo a PHI value


async def test_missing_required_field_outside_patient_information_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """Regression: `missing_required` used to only inspect `patient_information`, so
    a `system_fields` target declared in any other section — e.g. `hospital_npi` /
    `doctor_name` — silently passed even when its section was omitted entirely from
    the intake payload. A fully-filled `patient_information` must no longer be
    enough to create the form."""
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": {
                "patient_information": {
                    "patient_name": "Jane Doe",
                    "patient_dob": "1990-04-12",
                    "patient_gender": "Female",
                },
                "appointment_information": {"appointment_date": "2026-08-03"},
                # insurance_information / insurance_reference_information /
                # verification_information / hospital_information /
                # provider_reference_information all omitted.
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 422, resp.text
    assert set(resp.json()["data"]["fields"]) == {
        "sections.patient_information.chart_number",
        "sections.insurance_information.policy_number",
        "sections.insurance_reference_information.insurance_provider_name",
        "sections.insurance_reference_information.insurance_phone_number",
        "sections.verification_information.verified_by",
        "sections.verification_information.callback_number",
        "sections.hospital_information.hospital_name",
        "sections.hospital_information.hospital_address",
        "sections.hospital_information.tax_id",
        "sections.hospital_information.npi",
        "sections.provider_reference_information.provider_name",
        "sections.provider_reference_information.npi",
    }


async def test_unknown_schema_version_returns_404(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
) -> None:
    form_type_id, _ = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(uuid7()),
            "intake_payload": INTAKE_PAYLOAD,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


async def test_version_belonging_to_other_form_type_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
) -> None:
    _, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(uuid7()),  # not the version's parent schema
            "schema_version_id": str(version_id),
            "intake_payload": INTAKE_PAYLOAD,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text


async def test_missing_key_returns_401(
    client: httpx.AsyncClient, ibv_schema: tuple[UUID, UUID]
) -> None:
    form_type_id, version_id = ibv_schema
    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": INTAKE_PAYLOAD,
        },
    )
    assert resp.status_code == 401, resp.text


async def test_wrong_scope_returns_403(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
) -> None:
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id, scope="calls:read")

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": INTAKE_PAYLOAD,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


async def _post_intake(
    client: httpx.AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    ibv_schema: tuple[UUID, UUID],
    payload: dict[str, object],
) -> httpx.Response:
    """POST a patient-form intake request with the given payload; shared by the intake
    payload-shape tests (unknown paths, phone auto-format/validation)."""
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)
    return await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": payload,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_unknown_field_paths_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """A payload with a key not in the schema's leaf set must be rejected 422."""
    bad_payload: dict[str, object] = {
        **INTAKE_PAYLOAD,
        "unknown_section": {"mystery_field": "oops"},
    }
    resp = await _post_intake(client, admin_sessionmaker, rbac_world, ibv_schema, bad_payload)

    assert resp.status_code == 422, resp.text
    assert "sections.unknown_section.mystery_field" in resp.json()["data"]["fields"]


async def test_doubly_nested_field_path_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """A mis-nested payload (extra 'sections' wrapper inside a section) is rejected."""
    bad_payload = {
        **INTAKE_PAYLOAD,
        "sections": {"patient_information": {"patient_name": "Re-wrapped"}},
    }
    resp = await _post_intake(client, admin_sessionmaker, rbac_world, ibv_schema, bad_payload)

    assert resp.status_code == 422, resp.text
    offending = resp.json()["data"]["fields"]
    assert any("sections.sections" in p for p in offending)


async def test_rls_isolation_other_tenant_cannot_see_row(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    form_type_id, version_id = ibv_schema
    token = await _issue_key(admin_sessionmaker, rbac_world.tenant_id)

    resp = await client.post(
        "/api/v1/patient-forms",
        json={
            "form_type_id": str(form_type_id),
            "schema_version_id": str(version_id),
            "intake_payload": INTAKE_PAYLOAD,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    form_id = UUID(resp.json()["data"]["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.other_tenant_id) as session:
        found = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one_or_none()
    assert found is None  # RLS hides tenant A's form from tenant B


async def test_upload_auto_formats_missing_plus_on_insurance_phone(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """The clinic-submitted number has no leading '+' — it must be added before
    storage, and both the promoted column and the raw field_answer must agree on the
    fixed-up value (2026-07-15 design doc)."""
    payload: dict[str, object] = {
        **INTAKE_PAYLOAD,
        "insurance_reference_information": {
            "insurance_provider_name": "Demo Health Plan",
            "insurance_phone_number": "15550100",  # no leading '+'
        },
    }
    resp = await _post_intake(client, admin_sessionmaker, rbac_world, ibv_schema, payload)
    assert resp.status_code == 200, resp.text
    form_id = UUID(resp.json()["data"]["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.tenant_id) as session:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one()
        assert form.insurance_provider_phone_number == "+15550100"

        answer = (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id,
                    FieldAnswer.field_path
                    == "sections.insurance_reference_information.insurance_phone_number",
                )
            )
        ).scalar_one()
        assert answer.value == {"value": "+15550100"}


async def test_upload_leaves_already_prefixed_phone_unchanged(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    """A number that already has a leading '+' is left exactly as submitted — no
    reformatting is applied beyond adding a missing '+' (2026-07-15 design doc)."""
    payload: dict[str, object] = {
        **INTAKE_PAYLOAD,
        "insurance_reference_information": {
            "insurance_provider_name": "Demo Health Plan",
            "insurance_phone_number": "+15559990000",
        },
    }
    resp = await _post_intake(client, admin_sessionmaker, rbac_world, ibv_schema, payload)
    assert resp.status_code == 200, resp.text
    form_id = UUID(resp.json()["data"]["id"])

    async with tenant_session(rls_sessionmaker, rbac_world.tenant_id) as session:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == form_id))
        ).scalar_one()
        assert form.insurance_provider_phone_number == "+15559990000"

        answer = (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id,
                    FieldAnswer.field_path
                    == "sections.insurance_reference_information.insurance_phone_number",
                )
            )
        ).scalar_one()
        assert answer.value == {"value": "+15559990000"}


async def test_upload_rejects_invalid_phone_even_after_adding_plus(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    ibv_schema: tuple[UUID, UUID],
    cleanup_forms: None,
) -> None:
    payload: dict[str, object] = {
        **INTAKE_PAYLOAD,
        "insurance_reference_information": {
            "insurance_provider_name": "Demo Health Plan",
            "insurance_phone_number": "555 000 1234",  # still invalid once '+' is added
        },
    }
    resp = await _post_intake(client, admin_sessionmaker, rbac_world, ibv_schema, payload)
    assert resp.status_code == 422, resp.text
    assert resp.json()["data"]["fields"] == [
        "sections.insurance_reference_information.insurance_phone_number"
    ]
