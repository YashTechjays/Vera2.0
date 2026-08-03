# Create New Patient Form (In-App Intake) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a tenant user with `forms:write` create a new patient form from the Data Management page: pick a form family, fill its latest published schema version in the existing dynamic renderer, submit.

**Architecture:** Backend — extract the API-key intake endpoint's creation logic into a shared helper, add a session-authed `POST /patient-forms:create` (server resolves the family's single published version) and a tenant-facing `GET /patient-forms/schemas` catalog list. Frontend — a new create mode on `IbvProvider` reuses `SchemaForm` unchanged; a two-step `CreatePatientFormModal` (pick family → fill form) plus an "Add patient form" button on Data Management. Spec: `docs/superpowers/specs/2026-07-12-create-patient-form-design.md`.

**Tech Stack:** FastAPI + SQLAlchemy async + pytest (backend, `vera-backend/`); React + TypeScript + Vite + vitest (frontend, `vera-frontend/`).

## Global Constraints

- The AppScript intake endpoint (`POST /patient-forms`, API-key auth) must stay byte-for-byte behavior-identical — its existing tests must pass unchanged.
- PHI discipline: never log/echo field **values**; validation errors carry field **paths** only; audit records carry names/counts/ids only. No PHI in URLs.
- Backend endpoints: return `ResponseModel[T]` via `ok(...)`, errors via `CustomAPIException` subclasses (never `HTTPException`), `Cache-Control: no-store` on responses.
- Repo convention: all endpoints return HTTP 200 with the envelope (the spec's "201" is amended to 200 for consistency with every existing endpoint, including the intake POST).
- No success toast: the frontend has no toast infrastructure; success feedback is the modal closing + the worklist refreshing (spec amendment, agreed direction "close + refresh").
- Backend gate: `cd vera-backend && just check` (ruff + mypy --strict + pytest). Integration tests need `just up && just migrate` (local Postgres) and `LOCAL_KMS_MASTER_KEY` set.
- Frontend gate: `cd vera-frontend && npx tsc -b && npx eslint src && npm test && npm run build`.
- Per repo `CLAUDE.md`: after implementation completes, run the **code-simplifier** agent, then re-run both gates (Task 9).
- Commit messages: no `Co-Authored-By` lines.

## File Structure

Backend (all inside `vera-backend/`):
- Modify `apps/control_plane/src/control_plane/api/v1/patient_forms.py` — extract `_create_patient_form` helper; add `POST /patient-forms:create`, `GET /patient-forms/schemas` (declared **before** `GET /patient-forms/{form_id}`).
- Modify `apps/control_plane/src/control_plane/api/v1/common.py` — new shared `published_schema_version` query helper.
- Modify `apps/control_plane/src/control_plane/api/v1/prompts.py` — delete its private `_published_schema_version`, import the shared one.
- Create `tests/integration/control_plane/test_patient_forms_create.py` — tests for both new endpoints.

Frontend (all inside `vera-frontend/`):
- Modify `src/lib/patient-forms/types.ts` — `IntakeSchemaOption`, `PatientFormCreateResult`.
- Modify `src/lib/patient-forms/api.ts` — `listIntakeSchemas()`, `createPatientForm()`.
- Create `src/lib/patient-forms/intake.ts` + `src/lib/patient-forms/intake.test.ts` — `valuesToIntakePayload` (flat values map → nested payload).
- Modify `src/lib/ibv/validation.ts` + create `src/lib/ibv/validation.test.ts` — `validateCreate` (system_fields-driven requiredness).
- Modify `src/components/ibv/IbvProvider.tsx` — create mode (`openCreate`/`beginCreate`/`submitCreate`).
- Create `src/components/ibv/CreatePatientFormModal.tsx` — two-step modal reusing `<SchemaForm />`.
- Modify `src/components/layout/AppShell.tsx` — mount the new modal.
- Modify `src/pages/DataManagement.tsx` — "Add patient form" button.

---

### Task 1: Backend — extract the shared creation helper

The API-key intake endpoint's body (validate → promote → insert `PatientForm` → insert `FieldAnswer` rows) becomes a reusable function. Behavior-preserving refactor: the existing intake tests are the regression proof.

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py` (endpoint body at lines 117–235)
- Test: `vera-backend/tests/integration/control_plane/test_patient_forms_intake.py` (existing, unchanged)

**Interfaces:**
- Consumes: existing module-private `_v2_doc`, `_promote_or_422`, and `vera_core.forms.intake` helpers.
- Produces: `async def _create_patient_form(session: AsyncSession, *, tenant_id: UUID, version: SchemaVersion, form_schema: FormSchema, intake_payload: dict[str, Any]) -> CreatedPatientForm` where `CreatedPatientForm` is a frozen dataclass with fields `response: PatientFormResponse`, `sections: list[str]`, `answer_count: int`. Task 3 calls this from the new create endpoint. Raises `CustomAPIException(VALIDATION_ERROR)` on missing required fields / bad promoted values.

- [ ] **Step 1: Confirm the existing intake tests pass before touching anything**

Run (Postgres must be up: `just up && just migrate` in `vera-backend/` if not already):
```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_patient_forms_intake.py -q
```
Expected: all tests PASS (they skip if Postgres is unreachable — bring it up rather than accepting skips).

- [ ] **Step 2: Add the helper and refactor `upload_patient_form` to call it**

In `patient_forms.py`, add imports at the top (merge into the existing import block):

```python
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
```

Insert directly above the `@router.post("/patient-forms", ...)` decorator (after `_promote_or_422`):

```python
@dataclass(frozen=True)
class CreatedPatientForm:
    """What both create paths (API-key intake, in-app create) hand back to their
    endpoint: the non-PHI ack payload plus the audit detail (section keys and
    answer count — names/counts only, never values)."""

    response: PatientFormResponse
    sections: list[str]
    answer_count: int


async def _create_patient_form(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    version: SchemaVersion,
    form_schema: FormSchema,
    intake_payload: dict[str, Any],
) -> CreatedPatientForm:
    """Validate + persist one new patient form: `missing_required` against the
    schema's `system_fields` (422 with paths), promote the typed worklist columns
    (422 on bad values), insert the `PatientForm` (status ready_for_processing)
    and one INTAKE-source `field_answer` per provided leaf. Shared verbatim by the
    API-key intake endpoint and the session-user create endpoint."""
    missing = missing_required(intake_payload, version.schema_json)
    if missing:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="missing required fields",
            data={"fields": missing},
        )
    doc = _v2_doc(version.schema_json)
    promoted = PromotedIdentifiers()
    if doc is not None:
        promoted = _promote_or_422(lambda p: resolve_path(intake_payload, p), doc)

    form = PatientForm(
        tenant_id=tenant_id,
        schema_version_id=version.id,
        status=FormStatus.READY_FOR_PROCESSING.value,
        intake_payload=intake_payload,
        patient_name=promoted.patient_name,
        patient_dob=promoted.patient_dob,
        appointment_date=promoted.appointment_date,
        chart_number=promoted.chart_number,
        appointment_type=promoted.appointment_type,
        member_id=promoted.member_id,
        insurance_provider=promoted.insurance_provider,
        insurance_provider_phone_number=promoted.insurance_provider_phone_number,
        completion_pct=0,
        retry_count=0,
    )
    session.add(form)
    await session.flush()

    # Normalized intake answers: one INTAKE-source field_answer per provided leaf.
    # v2 documents use root-anchored paths (`sections.…` — spec §4.2), so the
    # payload (nested by section_key) is flattened under a `sections` root.
    payload_root = {"sections": intake_payload} if doc is not None else intake_payload
    answers = list(iter_leaf_answers(payload_root))
    session.add_all(
        FieldAnswer(
            tenant_id=tenant_id,
            form_id=form.id,
            call_id=None,
            field_path=path,
            value={"value": raw},
            source=AnswerSource.INTAKE.value,
            confidence=None,
            evidence_seq=None,
            evidence=None,
            is_current=True,
        )
        for path, raw in answers
    )

    await session.refresh(form)  # populate server-defaulted created_at
    return CreatedPatientForm(
        response=PatientFormResponse(
            id=form.id,
            status=form.status,
            insurance_type=form_schema.insurance_type,
            schema_version_id=form.schema_version_id,
            completion_pct=float(form.completion_pct),
            created_at=form.created_at,
        ),
        sections=sorted(key for key, value in intake_payload.items() if isinstance(value, dict)),
        answer_count=len(answers),
    )
```

Then replace the body of `upload_patient_form` from the `missing = missing_required(...)` line (currently line 153) down to the closing of the `async with` block (currently line 216, the `sections = sorted(...)` statement) with:

```python
        created = await _create_patient_form(
            session,
            tenant_id=principal.tenant_id,
            version=version,
            form_schema=form_schema,
            intake_payload=body.intake_payload,
        )
```

(The `row = ...` resolve/verify block at lines 137–151 stays — exact-version binding is intake-specific.) Update the audit emit + return after the `async with` block to read from `created`:

```python
    # Audit the PHI write after commit — field names/counts/ids only, never values.
    await get_audit(request).emit(
        AuditRecord(
            tenant_id=principal.tenant_id,
            actor_type=ActorType.SERVICE,
            actor_user_id=None,
            actor_label=str(principal.key_id),
            event_type=AuditEvent.FORM_INTAKE.value,
            resource_type="patient_form",
            resource_id=str(created.response.id),
            detail={
                "schema_version_id": str(body.schema_version_id),
                "sections": created.sections,
                "answer_count": created.answer_count,
            },
        )
    )
    return ok(created.response)
```

- [ ] **Step 3: Run the intake tests — must pass unchanged**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_patient_forms_intake.py -q
```
Expected: PASS, same test set as Step 1. Also run `just lint && just typecheck`. Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py
git commit -m "refactor(patient-forms): extract shared _create_patient_form helper from intake endpoint"
```

---

### Task 2: Backend — `GET /patient-forms/schemas` (tenant-facing family list)

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`
- Create: `vera-backend/tests/integration/control_plane/test_patient_forms_create.py`

**Interfaces:**
- Consumes: existing `TenantSession`, `require`, `ok`, models.
- Produces: `GET /patient-forms/schemas` gated `forms:read`, returning `list[IntakeSchemaOption]` with fields `schema_id: UUID`, `name: str`, `insurance_type: str`, `published_version_id: UUID`, `published_version: int`. Task 5's frontend `listIntakeSchemas()` mirrors this shape. **Route-order invariant:** declared before `GET /patient-forms/{form_id}` or FastAPI matches `schemas` as a form id.

- [ ] **Step 1: Write the failing tests**

Create `vera-backend/tests/integration/control_plane/test_patient_forms_create.py`:

```python
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
    resp = await client.get(
        "/api/v1/patient-forms/schemas", headers=_auth(rbac_world.admin_token)
    )
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
    resp = await client.get(
        "/api/v1/patient-forms/schemas", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403, resp.text
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_patient_forms_create.py -q
```
Expected: FAIL — `test_list_schemas_returns_published_families` gets a 422 (the path falls through to `GET /patient-forms/{form_id}` with `form_id="schemas"`).

- [ ] **Step 3: Implement the endpoint**

In `patient_forms.py`, add `VersionStatus` to the existing `vera_core.models.enums` import. Then insert **between** `list_patient_forms` and `get_patient_form` (route order matters — `/patient-forms/schemas` must be registered before `/patient-forms/{form_id}`):

```python
class IntakeSchemaOption(BaseModel):
    """One form family selectable for in-app intake — global catalog data only
    (the form template, never patient values)."""

    schema_id: UUID
    name: str
    insurance_type: str
    published_version_id: UUID
    published_version: int


# NOTE: declared before GET /patient-forms/{form_id} — FastAPI matches routes in
# declaration order, and the parameterized route would otherwise swallow "schemas".
@router.get(
    "/patient-forms/schemas",
    response_model=ResponseModel[list[IntakeSchemaOption]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_intake_schemas(
    response: Response,
    session: TenantSession,
    caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[list[IntakeSchemaOption]]:
    """The form families a tenant user can create a patient form from — only
    families with a published version (the in-app create path binds to it).
    Reads the global catalog only (no patient data), so no PHI-access audit —
    same stance as GET /schema-versions/{version_id}."""
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.execute(
            select(FormSchema, SchemaVersion)
            .join(SchemaVersion, SchemaVersion.schema_id == FormSchema.id)
            .where(SchemaVersion.status == VersionStatus.PUBLISHED.value)
            .order_by(FormSchema.name)
        )
    ).all()
    return ok(
        [
            IntakeSchemaOption(
                schema_id=form_schema.id,
                name=form_schema.name,
                insurance_type=form_schema.insurance_type,
                published_version_id=version.id,
                published_version=version.version,
            )
            for form_schema, version in rows
        ]
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_patient_forms_create.py -q
```
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py vera-backend/tests/integration/control_plane/test_patient_forms_create.py
git commit -m "feat(patient-forms): tenant-facing GET /patient-forms/schemas lists published form families"
```

---

### Task 3: Backend — `POST /patient-forms:create` + shared `published_schema_version`

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/common.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py` (delete `_published_schema_version` at lines 119–127; update its call sites)
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py`
- Test: `vera-backend/tests/integration/control_plane/test_patient_forms_create.py`

**Interfaces:**
- Consumes: Task 1's `_create_patient_form` / `CreatedPatientForm`.
- Produces: `POST /patient-forms:create` gated `forms:write`, body `{schema_id: UUID, intake_payload: dict}`, returns the existing `PatientFormResponse` shape (`id`, `status`, `insurance_type`, `schema_version_id`, `completion_pct`, `created_at`) in the envelope. 404 unknown `schema_id`; 409 no published version; 422 missing required fields (`data.fields` = paths). Also: `async def published_schema_version(session: AsyncSession, schema_id: UUID) -> SchemaVersion | None` in `common.py`, used by prompts + this endpoint. Task 5's frontend `createPatientForm()` mirrors the request/response.

- [ ] **Step 1: Write the failing tests**

Append to `test_patient_forms_create.py`:

```python
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
```

Note on `len(answers) == 14`: `INTAKE_PAYLOAD` provides exactly 14 leaves (count them in the dict). If the seeded schema's leaf set changes this payload, mirror whatever `test_upload_creates_form_and_intake_answers` in `test_patient_forms_intake.py` asserts.

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_patient_forms_create.py -q
```
Expected: the new `test_create_*` tests FAIL with 404s (route doesn't exist; FastAPI returns 404 for an unknown path — the 404-expecting test may accidentally pass, that's fine at this stage); Task 2's tests still PASS.

- [ ] **Step 3: Move `published_schema_version` into `common.py`**

In `common.py`, extend the `vera_core.models` import with `SchemaVersion` and add `from vera_core.models.enums import AuthEvent, VersionStatus` (merge with the existing `AuthEvent` import). Add at the bottom:

```python
async def published_schema_version(
    session: AsyncSession, schema_id: UUID
) -> SchemaVersion | None:
    """The form family's single published version, or None. At most one exists —
    the `uq_schema_version_published_per_schema` partial unique index."""
    return (
        await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == schema_id,
                SchemaVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
```

In `prompts.py`: delete the `_published_schema_version` function (lines 119–127), add `published_schema_version` to its `control_plane.api.v1.common` import (or add that import if absent), and rename every call site (`grep -n "_published_schema_version" apps/control_plane/src/control_plane/api/v1/prompts.py` — update each to `published_schema_version`). Remove any now-unused imports (`VersionStatus` if nothing else in the file uses it).

Run the prompt tests to prove the move is behavior-preserving:
```bash
cd vera-backend && uv run pytest tests/integration/control_plane -k prompt -q
```
Expected: PASS.

- [ ] **Step 4: Implement the create endpoint**

In `patient_forms.py`: add `ConflictError` to the `control_plane.exceptions` import and `published_schema_version` to the `control_plane.api.v1.common` import line. Insert directly after `upload_patient_form` (before the display section):

```python
class PatientFormCreateRequest(BaseModel):
    schema_id: UUID  # form_schema.id — the server binds to its published version
    intake_payload: dict[str, Any]  # nested by section_key


@router.post(
    "/patient-forms:create",
    response_model=ResponseModel[PatientFormResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def create_patient_form(
    body: PatientFormCreateRequest,
    request: Request,
    response: Response,
    session: TenantSession,
    tenant_id: TenantId,
    caller: VerifiedIdentity = require("forms:write"),
) -> ResponseModel[PatientFormResponse]:
    """In-app patient-form creation (Data Management). Unlike the API-key intake
    path — which binds to the exact version the sheet was generated from — the
    caller picks only the form family; the server resolves and binds its single
    published version, so an in-app form can never be created against a draft."""
    response.headers["Cache-Control"] = "no-store"
    form_schema = (
        await session.execute(select(FormSchema).where(FormSchema.id == body.schema_id))
    ).scalar_one_or_none()
    if form_schema is None:
        raise NotFoundError(message="unknown form schema")
    version = await published_schema_version(session, body.schema_id)
    if version is None:
        # E.g. demoted between the picker fetch and this submit.
        raise ConflictError(message="this form schema has no published version")

    created = await _create_patient_form(
        session,
        tenant_id=tenant_id,
        version=version,
        form_schema=form_schema,
        intake_payload=body.intake_payload,
    )

    # PHI write by a session user — same FORM_INTAKE event as the sheet path,
    # attributed to the user. Field names/counts/ids only, never values.
    await get_audit(request).emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.FORM_INTAKE.value,
            resource_type="patient_form",
            resource_id=str(created.response.id),
            detail={
                "schema_version_id": str(created.response.schema_version_id),
                "sections": created.sections,
                "answer_count": created.answer_count,
            },
        )
    )
    return ok(created.response, message="Patient form created.")
```

- [ ] **Step 5: Run the full new test file, then the whole backend gate**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_patient_forms_create.py tests/integration/control_plane/test_patient_forms_intake.py -q
cd vera-backend && just check
```
Expected: all PASS; `just check` clean (ruff + mypy --strict + full pytest).

- [ ] **Step 6: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/patient_forms.py vera-backend/apps/control_plane/src/control_plane/api/v1/common.py vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py vera-backend/tests/integration/control_plane/test_patient_forms_create.py
git commit -m "feat(patient-forms): session-authed POST /patient-forms:create binds the published schema version"
```

---

### Task 4: Frontend — API types + client functions

**Files:**
- Modify: `vera-frontend/src/lib/patient-forms/types.ts`
- Modify: `vera-frontend/src/lib/patient-forms/api.ts`

**Interfaces:**
- Consumes: backend contracts from Tasks 2–3.
- Produces: `IntakeSchemaOption` type; `PatientFormCreateResult` type; `listIntakeSchemas(): Promise<IntakeSchemaOption[]>`; `createPatientForm(schemaId: string, intakePayload: Record<string, unknown>): Promise<PatientFormCreateResult>`. Tasks 7–8 consume all four.

- [ ] **Step 1: Add the types**

Append to `types.ts`:

```ts
/** GET /patient-forms/schemas — a form family selectable for in-app intake
 *  (only families with a published version are returned). Catalog data, not PHI. */
export type IntakeSchemaOption = {
  schema_id: string
  name: string
  insurance_type: string
  published_version_id: string
  published_version: number
}

/** Non-PHI ack returned by POST /patient-forms:create. */
export type PatientFormCreateResult = {
  id: string
  status: PatientFormStatus
  insurance_type: string
  schema_version_id: string
  completion_pct: number
  created_at: string
}
```

- [ ] **Step 2: Add the client functions**

In `api.ts`, extend the type import with `IntakeSchemaOption` and `PatientFormCreateResult`, then append:

```ts
/** GET /patient-forms/schemas — form families with a published version. */
export function listIntakeSchemas(): Promise<IntakeSchemaOption[]> {
  return apiRequest<IntakeSchemaOption[]>("/patient-forms/schemas")
}

/** POST /patient-forms:create — create a patient form from a family's published
 *  schema version (resolved server-side; the client never picks a version). */
export function createPatientForm(
  schemaId: string,
  intakePayload: Record<string, unknown>,
): Promise<PatientFormCreateResult> {
  return apiRequest<PatientFormCreateResult>("/patient-forms:create", {
    method: "POST",
    body: { schema_id: schemaId, intake_payload: intakePayload },
  })
}
```

- [ ] **Step 3: Typecheck and commit**

```bash
cd vera-frontend && npx tsc -b
git add src/lib/patient-forms/types.ts src/lib/patient-forms/api.ts
git commit -m "feat(patient-forms): API client for intake schema list and in-app create"
```
Expected: `tsc` clean.

---

### Task 5: Frontend — `valuesToIntakePayload` (TDD)

The renderer's flat values map (root-anchored `sections.<key>...` paths) → the nested-by-section `intake_payload` the create endpoint expects (no `sections` root — the backend adds that itself when flattening).

**Files:**
- Create: `vera-frontend/src/lib/patient-forms/intake.ts`
- Test: `vera-frontend/src/lib/patient-forms/intake.test.ts`

**Interfaces:**
- Consumes: `FormValues` (`Record<string, string>`) from `@/lib/ibv/types`.
- Produces: `valuesToIntakePayload(values: FormValues): Record<string, unknown>`. Task 7's `submitCreate` consumes it.

- [ ] **Step 1: Write the failing tests**

Create `intake.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import { valuesToIntakePayload } from "./intake"

describe("valuesToIntakePayload", () => {
  it("nests values by section, stripping the sections root", () => {
    expect(
      valuesToIntakePayload({
        "sections.patient_information.patient_name": "Jane Doe",
        "sections.patient_information.patient_dob": "1990-04-12",
        "sections.insurance_information.policy_number": "POL-1",
      }),
    ).toEqual({
      patient_information: { patient_name: "Jane Doe", patient_dob: "1990-04-12" },
      insurance_information: { policy_number: "POL-1" },
    })
  })

  it("recurses into nested groups", () => {
    expect(
      valuesToIntakePayload({
        "sections.benefits.ivf.cycle_limit": "3",
      }),
    ).toEqual({ benefits: { ivf: { cycle_limit: "3" } } })
  })

  it("skips empty and whitespace-only values, trimming the rest", () => {
    expect(
      valuesToIntakePayload({
        "sections.a.filled": "  x  ",
        "sections.a.blank": "   ",
        "sections.a.empty": "",
      }),
    ).toEqual({ a: { filled: "x" } })
  })

  it("ignores paths outside the sections namespace", () => {
    expect(valuesToIntakePayload({ stray: "x" })).toEqual({})
  })
})
```

- [ ] **Step 2: Run to verify failure**

```bash
cd vera-frontend && npx vitest run src/lib/patient-forms/intake.test.ts
```
Expected: FAIL — cannot resolve `./intake`.

- [ ] **Step 3: Implement**

Create `intake.ts`:

```ts
// The inverse of the backend intake flattener (`iter_leaf_answers`): the
// renderer's flat values map (root-anchored `sections.<key>...` paths) becomes
// the nested-by-section intake_payload POST /patient-forms:create expects.
// Empty values are omitted — the backend treats blank as "not provided".

import type { FormValues } from "@/lib/ibv/types"

const SECTIONS_PREFIX = "sections."

export function valuesToIntakePayload(values: FormValues): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  for (const [path, raw] of Object.entries(values)) {
    const value = (raw ?? "").trim()
    if (value === "" || !path.startsWith(SECTIONS_PREFIX)) continue
    const parts = path.slice(SECTIONS_PREFIX.length).split(".")
    let node = payload
    for (const part of parts.slice(0, -1)) {
      const next = node[part]
      node =
        typeof next === "object" && next !== null
          ? (next as Record<string, unknown>)
          : ((node[part] = {}) as Record<string, unknown>)
    }
    node[parts[parts.length - 1]] = value
  }
  return payload
}
```

- [ ] **Step 4: Run to verify pass, commit**

```bash
cd vera-frontend && npx vitest run src/lib/patient-forms/intake.test.ts && npx tsc -b
git add src/lib/patient-forms/intake.ts src/lib/patient-forms/intake.test.ts
git commit -m "feat(patient-forms): valuesToIntakePayload builds the nested create payload"
```
Expected: 4 tests PASS, tsc clean.

---

### Task 6: Frontend — `validateCreate` (TDD)

Create-mode validation: requiredness comes from the schema's `system_fields` block (mirroring backend `required_intake_fields` — targets without a declared `default`), **not** a leaf's own `required` (which governs voice collection). Filled fields keep the pattern/date-format/range checks. Backend parity note: `missing_required` does not consider applicability, so neither does the required pass here.

**Files:**
- Modify: `vera-frontend/src/lib/ibv/validation.ts`
- Test: `vera-frontend/src/lib/ibv/validation.test.ts` (new)

**Interfaces:**
- Consumes: existing private `validateLeaf`, `allLeaves`, `isApplicable`, plus `systemFieldPaths` from `./schema` (add to the existing import).
- Produces: `validateCreate(schema: FormSchema, values: FormValues, opts?: { includeRequired?: boolean }): ValidationErrors`. Task 7 consumes it (`includeRequired: false` before the first submit attempt, so an untouched form isn't a wall of red).

- [ ] **Step 1: Write the failing tests**

Create `validation.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import { validateCreate } from "./validation"
import type { FormSchema } from "./types"

// Minimal v2 document: one system field without a default (required at create),
// one with a default (exempt), and a voice-required leaf (NOT required at create).
const schema = {
  dsl_version: "2.1",
  name: "Test Form",
  sections: {
    patient_information: {
      title: "Patient Information",
      fields: {
        patient_name: { type: "text", title: "Patient Name" },
        patient_gender: {
          type: "enum",
          title: "Gender",
          values: ["Female", "Male"],
          default: "N/A",
        },
        health_plan: {
          type: "text",
          title: "Health Plan",
          required: true,
          validation: { pattern: "^[A-Z].*$" },
        },
      },
    },
  },
  system_fields: {
    patient_name: "sections.patient_information.patient_name",
    patient_gender: "sections.patient_information.patient_gender",
  },
} as unknown as FormSchema

const NAME = "sections.patient_information.patient_name"
const GENDER = "sections.patient_information.patient_gender"
const PLAN = "sections.patient_information.health_plan"

describe("validateCreate", () => {
  it("requires system_fields targets without a default; ignores voice-required leaves", () => {
    const errors = validateCreate(schema, {})
    expect(errors[NAME]).toBe("Patient Name is required")
    expect(errors[GENDER]).toBeUndefined() // default counts as filled
    expect(errors[PLAN]).toBeUndefined() // leaf `required` = voice collection, not intake
  })

  it("clears the required error once the system field is filled", () => {
    expect(validateCreate(schema, { [NAME]: "Jane" })).toEqual({})
  })

  it("still format-checks any filled field", () => {
    const errors = validateCreate(schema, { [NAME]: "Jane", [PLAN]: "bad" })
    expect(errors[PLAN]).toBe("Health Plan is invalid")
  })

  it("returns format errors only when includeRequired is false", () => {
    const errors = validateCreate(schema, { [PLAN]: "bad" }, { includeRequired: false })
    expect(errors[PLAN]).toBe("Health Plan is invalid")
    expect(errors[NAME]).toBeUndefined()
  })
})
```

- [ ] **Step 2: Run to verify failure**

```bash
cd vera-frontend && npx vitest run src/lib/ibv/validation.test.ts
```
Expected: FAIL — `validateCreate` is not exported.

- [ ] **Step 3: Implement**

In `validation.ts`, extend the `./schema` import to `import { allLeaves, isApplicable, isRequired, systemFieldPaths } from "./schema"`, then append:

```ts
/**
 * Create-mode validation (new patient form): requiredness comes from the
 * schema's `system_fields` block — the backend's `required_intake_fields` rule
 * (targets without a declared default), NOT a leaf's own `required`, which
 * governs voice collection. Filled fields still get the pattern / date-format /
 * range checks. Backend parity: `missing_required` ignores applicability for
 * system fields, so the required pass here does too. `includeRequired: false`
 * yields format errors only (shown live before the first submit attempt).
 */
export function validateCreate(
  schema: FormSchema,
  values: FormValues,
  { includeRequired = true }: { includeRequired?: boolean } = {},
): ValidationErrors {
  const errors: ValidationErrors = {}
  for (const leaf of allLeaves(schema)) {
    if (!isApplicable(schema, leaf.gates, values)) continue
    if ((values[leaf.path] ?? "").trim() === "") continue
    const message = validateLeaf(schema, leaf, values)
    if (message) errors[leaf.path] = message
  }
  if (!includeRequired) return errors
  const byPath = new Map(allLeaves(schema).map((l) => [l.path, l]))
  for (const path of systemFieldPaths(schema)) {
    const leaf = byPath.get(path)
    if (!leaf || leaf.field.default !== undefined) continue
    if ((values[path] ?? "").trim() === "" && !errors[path]) {
      errors[path] = `${leaf.field.title} is required`
    }
  }
  return errors
}
```

(`validateLeaf` stays private — same module. Its empty-value branch never runs here because empty values are skipped before the call.)

- [ ] **Step 4: Run to verify pass, commit**

```bash
cd vera-frontend && npx vitest run src/lib/ibv --silent=false && npx tsc -b
git add src/lib/ibv/validation.ts src/lib/ibv/validation.test.ts
git commit -m "feat(ibv): validateCreate — system_fields-driven create-time validation"
```
Expected: new tests + existing `conditions.test.ts` PASS, tsc clean.

---

### Task 7: Frontend — `IbvProvider` create mode

**Files:**
- Modify: `vera-frontend/src/components/ibv/IbvProvider.tsx`

**Interfaces:**
- Consumes: Task 4's `listIntakeSchemas`/`createPatientForm` types + functions, Task 5's `valuesToIntakePayload`, Task 6's `validateCreate`, existing `loadSchema`/`seed`/`allLeaves`.
- Produces: new context members (Task 8 consumes them):
  - `createModalOpen: boolean`
  - `openCreate: () => void` — reset to step 1 (also serves as the modal's Back action)
  - `closeCreate: () => void`
  - `beginCreate: (option: IntakeSchemaOption) => Promise<void>` — load published schema, seed defaults → step 2
  - `createSelection: IntakeSchemaOption | null` — null = picker step
  - `createSubmitting: boolean`, `createError: string | null`
  - `submitCreate: () => Promise<void>` — validate, POST, close + bump `savedTick`
  - Existing `schema`, `values`, `setValue`, `errors`, `loading` are reused by `<SchemaForm />` in create mode; `errors` switches to `validateCreate` (required errors gated behind the first submit attempt).

- [ ] **Step 1: Extend imports and the context type**

Add imports:

```ts
import { validateAll, validateCreate, type ValidationErrors } from "@/lib/ibv/validation"
import { allLeaves } from "@/lib/ibv/schema"
import { valuesToIntakePayload } from "@/lib/patient-forms/intake"
import { createPatientForm, /* …existing… */ } from "@/lib/patient-forms/api"
import type { IntakeSchemaOption, /* …existing… */ } from "@/lib/patient-forms/types"
```

(Merge into the existing import statements — `validateAll` and the patient-forms imports already exist.)

Extend `Mode`:

```ts
type Mode = "mock" | "api" | "create"
```

Add to `IbvContextValue` (after `closeForm`):

```ts
  /** In-app create flow (Data Management → Add patient form). */
  createModalOpen: boolean
  /** Open the create modal at the schema-picker step (also the Back action). */
  openCreate: () => void
  closeCreate: () => void
  /** Bind the picked family: load its published schema, seed leaf defaults. */
  beginCreate: (option: IntakeSchemaOption) => Promise<void>
  /** The picked family, or null while still on the picker step. */
  createSelection: IntakeSchemaOption | null
  createSubmitting: boolean
  /** Modal-level create failure (stale published version, network) — a banner. */
  createError: string | null
  submitCreate: () => Promise<void>
```

- [ ] **Step 2: Add state and the create callbacks**

New state (next to the existing `useState` block):

```ts
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [createSelection, setCreateSelection] = useState<IntakeSchemaOption | null>(null)
  const [createAttempted, setCreateAttempted] = useState(false)
  const [createSubmitting, setCreateSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
```

Replace the `errors` memo:

```ts
  const errors: ValidationErrors = useMemo(() => {
    if (!schema) return {}
    // Create mode: requiredness comes from system_fields; the required errors
    // only show once a submit was attempted (format errors always show live).
    if (mode === "create")
      return validateCreate(schema, values, { includeRequired: createAttempted })
    return validateAll(schema, values)
  }, [schema, values, mode, createAttempted])
```

Add the callbacks (after `closeForm`):

```ts
  // Create path: step 1 (picker) has no schema; beginCreate loads the published
  // document and seeds declared defaults so what the user sees is what submits.
  const openCreate = useCallback(() => {
    setMode("create")
    setFormId(null)
    setError(null)
    setLoading(false)
    setStatus(null)
    setStatusError(null)
    setInsuranceType(null)
    setSchema(null)
    setCreateSelection(null)
    setCreateAttempted(false)
    setCreateError(null)
    seed({}, {}, null)
    setCreateModalOpen(true)
  }, [seed])

  const closeCreate = useCallback(() => setCreateModalOpen(false), [])

  const beginCreate = useCallback(
    async (option: IntakeSchemaOption) => {
      setCreateError(null)
      setLoading(true)
      try {
        const loaded = await loadSchema(option.published_version_id)
        const defaults: FormValues = {}
        for (const leaf of allLeaves(loaded)) {
          if (leaf.field.default !== undefined) defaults[leaf.path] = leaf.field.default
        }
        seed(defaults, {}, null)
        setSchema(loaded)
        setInsuranceType(option.insurance_type)
        setCreateSelection(option)
      } catch (err) {
        // ApiError and the parseSchema dsl_version guard both carry a
        // human-readable, non-PHI message.
        setCreateError(
          err instanceof Error ? err.message : "Could not load this form schema.",
        )
      } finally {
        setLoading(false)
      }
    },
    [seed],
  )

  const submitCreate = useCallback(async () => {
    if (!schema || !createSelection) return
    setCreateAttempted(true)
    if (Object.keys(validateCreate(schema, values)).length > 0) {
      setCreateError("Fill the required fields before submitting.")
      return
    }
    setCreateError(null)
    setCreateSubmitting(true)
    try {
      await createPatientForm(createSelection.schema_id, valuesToIntakePayload(values))
      setCreateModalOpen(false)
      setSavedTick((t) => t + 1) // worklist refetches; the new row is the feedback
    } catch (err) {
      // e.g. 409 "this form schema has no published version" (demoted mid-flow),
      // or the backend's authoritative 422 — surfaced as the modal banner.
      setCreateError(
        err instanceof ApiError ? err.message : "Could not create the patient form.",
      )
    } finally {
      setCreateSubmitting(false)
    }
  }, [schema, createSelection, values])
```

Add all eight new members to the `value: IbvContextValue` object literal.

- [ ] **Step 3: Typecheck + existing tests, commit**

```bash
cd vera-frontend && npx tsc -b && npx eslint src/components/ibv/IbvProvider.tsx && npm test
git add src/components/ibv/IbvProvider.tsx
git commit -m "feat(ibv): IbvProvider create mode — openCreate/beginCreate/submitCreate"
```
Expected: all clean/PASS.

---

### Task 8: Frontend — `CreatePatientFormModal` + Data Management button

**Files:**
- Create: `vera-frontend/src/components/ibv/CreatePatientFormModal.tsx`
- Modify: `vera-frontend/src/components/layout/AppShell.tsx` (mount next to `<IbvFormModal />`, line 52)
- Modify: `vera-frontend/src/pages/DataManagement.tsx` (button in the Card header row)

**Interfaces:**
- Consumes: Task 7's context members, Task 4's `listIntakeSchemas`, existing `<SchemaForm />`, `humanizeSegment` from `@/lib/patient-forms/display`.
- Produces: the user-facing flow. No new exports beyond the component.

- [ ] **Step 1: Create the modal component**

Create `CreatePatientFormModal.tsx`:

```tsx
import { useEffect, useState } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Select } from "@/components/ui/select"
import { listIntakeSchemas } from "@/lib/patient-forms/api"
import type { IntakeSchemaOption } from "@/lib/patient-forms/types"
import { humanizeSegment } from "@/lib/patient-forms/display"
import { useIbv } from "./IbvProvider"
import { SchemaForm } from "./SchemaForm"

/** Two-step create flow: pick a form family (only families with a published
 *  version are offered), then fill its published schema in the same renderer
 *  the review modal uses. Values are held in IbvProvider create mode. */
export function CreatePatientFormModal() {
  const {
    createModalOpen,
    openCreate,
    closeCreate,
    beginCreate,
    createSelection,
    createSubmitting,
    createError,
    submitCreate,
    schema,
    loading,
  } = useIbv()

  const [options, setOptions] = useState<IntakeSchemaOption[] | null>(null)
  const [optionsError, setOptionsError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState("")

  // (Re)load the selectable families on each open — cheap catalog read, and a
  // fresh list avoids offering a family whose version was demoted meanwhile.
  useEffect(() => {
    if (!createModalOpen) return
    setOptions(null)
    setOptionsError(null)
    setSelectedId("")
    let cancelled = false
    listIntakeSchemas()
      .then((res) => {
        if (!cancelled) setOptions(res)
      })
      .catch((err) => {
        if (!cancelled)
          setOptionsError(
            err instanceof Error ? err.message : "Could not load form schemas.",
          )
      })
    return () => {
      cancelled = true
    }
  }, [createModalOpen])

  const picking = createSelection === null

  return (
    <Dialog open={createModalOpen} onOpenChange={(o) => (o ? null : closeCreate())}>
      <DialogContent
        showCloseButton
        className={
          picking
            ? "flex max-h-[92vh] flex-col gap-0 p-0 sm:max-w-[480px]"
            : "flex max-h-[92vh] w-[96vw] max-w-[1200px] flex-col gap-0 p-0"
        }
      >
        <DialogHeader className="border-b border-border p-4">
          {createSelection && (
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {humanizeSegment(createSelection.insurance_type)}
            </p>
          )}
          <DialogTitle>
            {picking ? "Add Patient Form" : schema?.name ?? "New Patient Form"}
          </DialogTitle>
          <DialogDescription>
            {picking
              ? "Choose the form type to create."
              : "Fill in the patient details. Fields marked as system fields are required."}
          </DialogDescription>
        </DialogHeader>

        {picking ? (
          <div className="space-y-4 p-4">
            {optionsError && (
              <p className="text-sm text-destructive" role="alert">
                {optionsError}
              </p>
            )}
            {options === null && !optionsError && (
              <p className="text-sm text-muted-foreground">Loading…</p>
            )}
            {options?.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No published form schemas are available yet.
              </p>
            )}
            {options && options.length > 0 && (
              <Select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
                <option value="">Select a form schema…</option>
                {options.map((o) => (
                  <option key={o.schema_id} value={o.schema_id}>
                    {o.name} ({humanizeSegment(o.insurance_type)})
                  </option>
                ))}
              </Select>
            )}
            {createError && (
              <p className="text-sm text-destructive" role="alert">
                {createError}
              </p>
            )}
            <div className="flex justify-end gap-3">
              <Button variant="outline" onClick={closeCreate}>
                Cancel
              </Button>
              <Button
                disabled={!selectedId || loading}
                onClick={() => {
                  const option = options?.find((o) => o.schema_id === selectedId)
                  if (option) void beginCreate(option)
                }}
              >
                {loading ? "Loading…" : "Continue"}
              </Button>
            </div>
          </div>
        ) : (
          <>
            {createError && (
              <p
                className="border-b border-border bg-destructive/5 px-4 py-2 text-sm text-destructive"
                role="alert"
              >
                {createError}
              </p>
            )}
            <div className="flex-1 overflow-auto bg-[#f8f9fa] p-4 font-ibv">
              <SchemaForm />
            </div>
            <div className="flex items-center justify-between gap-4 border-t border-border p-4">
              <Button variant="outline" onClick={openCreate} disabled={createSubmitting}>
                Back
              </Button>
              <div className="flex items-center gap-3">
                <Button
                  variant="outline"
                  onClick={closeCreate}
                  disabled={createSubmitting}
                  className="min-w-[140px] border-ibv-row bg-white text-foreground hover:bg-muted/50"
                >
                  Cancel
                </Button>
                <Button
                  onClick={() => void submitCreate()}
                  disabled={createSubmitting}
                  className="min-w-[140px]"
                >
                  {createSubmitting ? "Submitting…" : "Submit"}
                </Button>
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 2: Mount it in `AppShell.tsx`**

Add the import next to the `IbvFormModal` import (line 7):

```tsx
import { CreatePatientFormModal } from "@/components/ibv/CreatePatientFormModal"
```

And render it directly after `<IbvFormModal />` (line 52):

```tsx
      <IbvFormModal />
      <CreatePatientFormModal />
```

- [ ] **Step 3: Add the button to `DataManagement.tsx`**

Update the lucide import (line 2):

```tsx
import { ArrowDown, ArrowUp, Plus, Search } from "lucide-react"
```

Update the hooks (lines 59–60):

```tsx
  const canRead = usePermission("forms:read")
  const canWrite = usePermission("forms:write")
  const { openFormById, openCreate, savedTick } = useIbv()
```

In the Card header's right-hand `<div className="flex items-center gap-3">` (line 161), append after the status-filter `<div className="w-44">…</div>`:

```tsx
            {canWrite && (
              <Button size="sm" onClick={openCreate}>
                <Plus className="size-4" />
                Add patient form
              </Button>
            )}
```

- [ ] **Step 4: Full frontend gate, commit**

```bash
cd vera-frontend && npx tsc -b && npx eslint src && npm test && npm run build
git add src/components/ibv/CreatePatientFormModal.tsx src/components/layout/AppShell.tsx src/pages/DataManagement.tsx
git commit -m "feat(data-management): Add patient form button + two-step create modal"
```
Expected: all clean/PASS/build succeeds.

---

### Task 9: Simplify, re-gate, verify end-to-end

**Files:** whatever the simplifier touches (re-run gates after).

- [ ] **Step 1: Run the code-simplifier** (repo-mandated: "simplify code" → `code-simplifier` agent from `code-simplifier@claude-plugins-official`) over the changes from Tasks 1–8.

- [ ] **Step 2: Re-run both gates**

```bash
cd vera-backend && just check
cd vera-frontend && npx tsc -b && npx eslint src && npm test && npm run build
```
Expected: clean. Commit any simplifier refinements:
```bash
git add -A && git commit -m "refactor: simplify create-patient-form implementation"
```
(Skip the commit if the simplifier changed nothing.)

- [ ] **Step 3: End-to-end verification** (the deliverable is observed behavior, not green tests)

1. Backend up: `cd vera-backend && just up && just migrate && just seed` (seed publishes schema versions), then `just api` (needs `LOCAL_KMS_MASTER_KEY`).
2. Frontend: `cd vera-frontend && npm run dev`.
3. In the browser: log in as a tenant user with `forms:write` → Data Management → **Add patient form** → pick a schema → verify the empty form renders with defaults pre-filled → Submit empty → inline "required" errors appear on system fields and nothing is created → fill the required fields → Submit → modal closes, the new row appears in the worklist with status **Ready for Processing** → open the row → the existing review modal renders the same values.
4. Negative check: a user without `forms:write` sees no Add button; `POST /api/v1/patient-forms:create` without auth → 401.

---

## Self-review notes (already applied)

- Spec coverage: shared helper (T1), schemas list + route order + test (T2), create endpoint + published resolution + 404/409/422/403/RLS tests (T3), API client (T4), unflatten (T5), create validation with default-exemption + voice-required exclusion (T6), provider create mode + error banner + double-submit guard via `createSubmitting` (T7), modal/button/empty-state (T8), simplify + gates + E2E (T9).
- Spec amendments carried into Global Constraints: HTTP 200 (not 201) for envelope consistency; no toast (no toast infra) — feedback is close + refresh, per the agreed post-create UX.
- Type consistency: `IntakeSchemaOption` field names identical across backend model (T2), TS type (T4), provider (T7), modal (T8); `beginCreate`/`submitCreate`/`openCreate`/`closeCreate` names match between T7's context type and T8's consumer.
