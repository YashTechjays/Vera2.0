"""IBV patient-form intake (spec §4.3) — the inbound, API-key-authenticated
ingress a Google Sheet (Apps Script) calls to upload one patient's intake payload.

Machine-to-machine, NOT the human display-path chain: authenticated by an
`intake:write` API key (tenant derived from the key, no user session). It binds the
new `PatientForm` to the exact `schema_version` the client names, validates the
payload against that version, promotes the searchable identifier columns, writes one
`INTAKE`-source `field_answer` row per provided leaf, audits the disclosure (field
names/counts only), and returns non-PHI metadata.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from control_plane.auth.api_key import ApiKeyPrincipal, require_scope
from control_plane.deps import get_audit, get_sessionmaker
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord
from vera_core.db import tenant_session
from vera_core.forms.intake import (
    InvalidIntakeValue,
    iter_leaf_answers,
    missing_required,
    promote_columns,
)
from vera_core.models import FieldAnswer, FormSchema, PatientForm, SchemaVersion
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import AnswerSource, FormStatus

router = APIRouter(tags=["patient-forms"])


class PatientFormUploadRequest(BaseModel):
    form_type_id: UUID  # form_schema.id — the IBV form family
    schema_version_id: UUID  # schema_version.id — exact version the sheet was built from
    intake_payload: dict[str, Any]  # nested by section_key


class PatientFormResponse(BaseModel):
    """Non-PHI acknowledgement — no patient values leave here."""

    id: UUID
    status: str
    insurance_type: str
    schema_version_id: UUID
    completion_pct: float
    created_at: datetime


@router.post(
    "/patient-forms",
    response_model=ResponseModel[PatientFormResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def upload_patient_form(
    body: PatientFormUploadRequest,
    request: Request,
    principal: ApiKeyPrincipal = require_scope("intake:write"),
) -> ResponseModel[PatientFormResponse]:
    async with tenant_session(get_sessionmaker(request), principal.tenant_id) as session:
        # Resolve + verify the client-supplied form/version (global catalog — the
        # tenant session can read it; published status is not required, we bind to
        # the exact version the sheet was generated from).
        # One round trip: the version and its true parent schema (FK-guaranteed).
        row = (
            await session.execute(
                select(SchemaVersion, FormSchema)
                .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
                .where(SchemaVersion.id == body.schema_version_id)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(message="unknown schema version")
        version, form_schema = row
        if version.schema_id != body.form_type_id:
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="schema version does not belong to that form type",
            )

        missing = missing_required(body.intake_payload, version.schema_json)
        if missing:
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="missing required fields",
                data={"fields": missing},
            )
        try:
            promoted = promote_columns(body.intake_payload)
        except InvalidIntakeValue as exc:
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="invalid field value",
                data={"fields": [exc.field_path]},
            ) from exc

        form = PatientForm(
            tenant_id=principal.tenant_id,
            schema_version_id=body.schema_version_id,
            status=FormStatus.READY_FOR_PROCESSING.value,
            intake_payload=body.intake_payload,
            patient_name=promoted.patient_name,
            patient_dob=promoted.patient_dob,
            appointment_date=promoted.appointment_date,
            chart_number=promoted.chart_number,
            member_id=promoted.member_id,
            completion_pct=0,
            retry_count=0,
        )
        session.add(form)
        await session.flush()

        # Normalized intake answers: one INTAKE-source field_answer per provided leaf.
        answers = list(iter_leaf_answers(body.intake_payload))
        session.add_all(
            FieldAnswer(
                tenant_id=principal.tenant_id,
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
        response = PatientFormResponse(
            id=form.id,
            status=form.status,
            insurance_type=form_schema.insurance_type,
            schema_version_id=form.schema_version_id,
            completion_pct=float(form.completion_pct),
            created_at=form.created_at,
        )
        sections = sorted(
            key for key, value in body.intake_payload.items() if isinstance(value, dict)
        )

    # Audit the PHI write after commit — field names/counts/ids only, never values.
    await get_audit(request).emit(
        AuditRecord(
            tenant_id=principal.tenant_id,
            actor_type=ActorType.SERVICE,
            actor_user_id=None,
            actor_label=str(principal.key_id),
            event_type=AuditEvent.FORM_INTAKE.value,
            resource_type="patient_form",
            resource_id=str(response.id),
            detail={
                "schema_version_id": str(body.schema_version_id),
                "sections": sections,
                "answer_count": len(answers),
            },
        )
    )
    return ok(response)
