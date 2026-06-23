"""IBV patient-form endpoints (spec §4.3).

Two caller classes share this router:
- **Intake** (`POST /patient-forms`) — machine-to-machine, authenticated by an
  `intake:write` API key (tenant from the key, no user session): creates a
  `PatientForm` + `INTAKE`-source `field_answer` rows from a published schema version.
- **Display + dispute resolution** (`GET /patient-forms`, `GET /patient-forms/{id}`,
  `POST /patient-forms/{id}/disputes:resolve`) — the logged-in frontend user, on the
  session display-path chain (`require(...)` → tenant-scoped RLS session → PHI-access
  audit → `Cache-Control: no-store`).

Every PHI response audits field **names** only (never values).
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from control_plane.api.v1.common import TenantId, TenantSession
from control_plane.auth.api_key import ApiKeyPrincipal, require_scope
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
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
from vera_core.forms.review import (
    AnswerRow,
    EvalRow,
    adjudication_action,
    build_field_views,
    completion_pct,
    unwrap_value,
)
from vera_core.models import (
    DisputeAction,
    FieldAnswer,
    FieldEvaluation,
    FormSchema,
    PatientForm,
    SchemaVersion,
)
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import AnswerSource, DisputeActionType, FormStatus

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


# ---------------------------------------------------------------------------
# Display + dispute resolution (session-authenticated, display-path chain)
# ---------------------------------------------------------------------------


class PatientFormSummary(BaseModel):
    """Worklist row — promoted identifiers + a few intake fields + counts."""

    id: UUID
    status: str
    patient_name: str | None
    chart_number: str | None
    appointment_date: date | None
    # Read from the intake snapshot (intake_payload), not promoted columns.
    appointment_type: str | None
    member_policy_id: str | None
    insurance_provider: str | None
    completion_pct: float
    dispute_count: int
    created_at: datetime
    updated_at: datetime


class PaginatedForms(BaseModel):
    items: list[PatientFormSummary]
    page: int
    page_size: int
    total: int


class DisputeView(BaseModel):
    previous_value: Any
    current_value: Any
    confidence: int | None
    evidence: str | None
    reasoning: str | None


class FieldView(BaseModel):
    field_path: str
    value: Any
    source: str
    confidence: int | None
    dispute: DisputeView | None


class PatientFormDetail(BaseModel):
    id: UUID
    status: str
    insurance_type: str
    schema_version_id: UUID
    completion_pct: float
    created_at: datetime
    updated_at: datetime
    patient_name: str | None
    chart_number: str | None
    appointment_date: date | None
    member_id: str | None
    fields: list[FieldView]


class ResolveRequest(BaseModel):
    form_data: dict[str, Any] = {}  # current value per dotted path (post-edit)
    dispute_fields: list[str] = []  # paths the reviewer explicitly accepted
    reasked_fields: list[str] = []  # paths to re-verify on the next call


def _audit_phi_read(
    request: Request, tenant_id: UUID, caller: VerifiedIdentity, resource_id: str, fields: list[str]
) -> AuditRecord:
    return AuditRecord(
        tenant_id=tenant_id,
        actor_type=ActorType.USER,
        actor_user_id=caller.user_id,
        actor_label=caller.email or caller.subject,
        event_type=AuditEvent.PHI_ACCESS.value,
        resource_type="patient_form",
        resource_id=resource_id,
        detail={"fields": fields},
    )


def _intake_field(form: PatientForm, section: str, field: str) -> str | None:
    """One value from a form's intake snapshot (flat `{section: {field: value}}`).
    Worklist display only — returns None when the section/field is absent."""
    section_obj = form.intake_payload.get(section)
    if not isinstance(section_obj, dict):
        return None
    value = section_obj.get(field)
    return None if value is None else str(value)


async def _unresolved_dispute_count_by_form(
    session: TenantSession, form_ids: list[UUID]
) -> dict[UUID, int]:
    """For each form, the number of current answers the judge flagged
    (`supported=false`) that no human has adjudicated yet."""
    if not form_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(FieldAnswer.form_id, func.count())
                .join(FieldEvaluation, FieldEvaluation.answer_id == FieldAnswer.id)
                .outerjoin(DisputeAction, DisputeAction.answer_id == FieldAnswer.id)
                .where(
                    FieldAnswer.form_id.in_(form_ids),
                    FieldAnswer.is_current.is_(True),
                    FieldEvaluation.supported.is_(False),
                    DisputeAction.id.is_(None),
                )
                .group_by(FieldAnswer.form_id)
            )
        )
        .tuples()
        .all()
    )
    return dict(rows)


async def _build_detail(session: TenantSession, form: PatientForm) -> PatientFormDetail:
    """Assemble the full review detail for one form (current answers + disputes)."""
    current = (
        (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form.id, FieldAnswer.is_current.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    answer_ids = [a.id for a in current]

    evals_by_answer: dict[UUID, EvalRow] = {}
    resolved_ids: set[UUID] = set()
    if answer_ids:
        # Latest evaluation per answer (newest wins).
        for ev in (
            await session.execute(
                select(FieldEvaluation)
                .where(FieldEvaluation.answer_id.in_(answer_ids))
                .order_by(FieldEvaluation.created_at.desc())
            )
        ).scalars():
            evals_by_answer.setdefault(
                ev.answer_id,
                EvalRow(supported=ev.supported, confidence=ev.confidence, evidence=ev.evidence),
            )
        resolved_ids = set(
            (
                await session.execute(
                    select(DisputeAction.answer_id).where(DisputeAction.answer_id.in_(answer_ids))
                )
            ).scalars()
        )

    # Most recent superseded value per path → the dispute's `previous_value`.
    prior_by_path: dict[str, Any] = {}
    for pa in (
        await session.execute(
            select(FieldAnswer)
            .where(FieldAnswer.form_id == form.id, FieldAnswer.is_current.is_(False))
            .order_by(FieldAnswer.created_at.desc())
        )
    ).scalars():
        prior_by_path.setdefault(pa.field_path, pa.value)

    form_schema = (
        await session.execute(
            select(FormSchema)
            .join(SchemaVersion, SchemaVersion.schema_id == FormSchema.id)
            .where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()

    views = build_field_views(
        [
            AnswerRow(
                id=a.id,
                field_path=a.field_path,
                value=a.value,
                source=a.source,
                confidence=a.confidence,
                evidence=a.evidence,
            )
            for a in current
        ],
        evals_by_answer,
        prior_by_path,
        resolved_answer_ids=resolved_ids,
    )
    return PatientFormDetail(
        id=form.id,
        status=form.status,
        insurance_type=form_schema.insurance_type,
        schema_version_id=form.schema_version_id,
        completion_pct=float(form.completion_pct),
        created_at=form.created_at,
        updated_at=form.updated_at,
        patient_name=form.patient_name,
        chart_number=form.chart_number,
        appointment_date=form.appointment_date,
        member_id=form.member_id,
        fields=[FieldView(**view) for view in views],
    )


@router.get(
    "/patient-forms",
    response_model=ResponseModel[PaginatedForms],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_patient_forms(
    request: Request,
    response: Response,
    session: TenantSession,
    tenant_id: TenantId,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    q: str | None = Query(None),
    caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[PaginatedForms]:
    response.headers["Cache-Control"] = "no-store"
    conds = []
    if status is not None:
        conds.append(PatientForm.status == status)
    if q:
        conds.append(PatientForm.patient_name.ilike(f"%{q.lower()}%"))

    total = (
        await session.execute(select(func.count()).select_from(PatientForm).where(*conds))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(PatientForm)
                .where(*conds)
                .order_by(PatientForm.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    counts = await _unresolved_dispute_count_by_form(session, [r.id for r in rows])
    items = [
        PatientFormSummary(
            id=r.id,
            status=r.status,
            patient_name=r.patient_name,
            chart_number=r.chart_number,
            appointment_date=r.appointment_date,
            appointment_type=_intake_field(r, "appointment_information", "appointment_type"),
            member_policy_id=_intake_field(r, "insurance_information", "policy_number"),
            insurance_provider=_intake_field(r, "insurance_reference_information", "insurance"),
            completion_pct=float(r.completion_pct),
            dispute_count=counts.get(r.id, 0),
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
    await get_audit(request).emit(
        _audit_phi_read(
            request,
            tenant_id,
            caller,
            "list",
            [
                "patient_name",
                "chart_number",
                "appointment_date",
                "appointment_type",
                "policy_number",
                "insurance",
            ],
        )
    )
    return ok(PaginatedForms(items=items, page=page, page_size=page_size, total=total))


@router.get(
    "/patient-forms/{form_id}",
    response_model=ResponseModel[PatientFormDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_patient_form(
    form_id: UUID,
    request: Request,
    response: Response,
    session: TenantSession,
    tenant_id: TenantId,
    caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[PatientFormDetail]:
    response.headers["Cache-Control"] = "no-store"
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")
    detail = await _build_detail(session, form)
    await get_audit(request).emit(
        _audit_phi_read(
            request, tenant_id, caller, str(form_id), [f.field_path for f in detail.fields]
        )
    )
    return ok(detail)


@router.post(
    "/patient-forms/{form_id}/disputes:resolve",
    response_model=ResponseModel[PatientFormDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def resolve_disputes(
    form_id: UUID,
    body: ResolveRequest,
    request: Request,
    response: Response,
    session: TenantSession,
    tenant_id: TenantId,
    caller: VerifiedIdentity = require("forms:write"),
) -> ResponseModel[PatientFormDetail]:
    response.headers["Cache-Control"] = "no-store"
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    current_by_path = {
        a.field_path: a
        for a in (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True)
                )
            )
        ).scalars()
    }
    # Historical values per edited path → OVERRIDE (swap-back) vs CORRECT detection.
    # Only the paths being edited need prior values, so don't load the whole history.
    priors_by_path: dict[str, list[Any]] = {}
    if body.form_data:
        for path, value in (
            await session.execute(
                select(FieldAnswer.field_path, FieldAnswer.value).where(
                    FieldAnswer.form_id == form_id,
                    FieldAnswer.field_path.in_(list(body.form_data.keys())),
                )
            )
        ).all():
            priors_by_path.setdefault(path, []).append(unwrap_value(value))

    dispute_fields = set(body.dispute_fields)
    changed: list[str] = []

    def _human_answer(path: str, raw: Any) -> FieldAnswer:
        return FieldAnswer(
            tenant_id=tenant_id,
            form_id=form_id,
            field_path=path,
            value={"value": raw},
            source=AnswerSource.HUMAN.value,
            is_current=True,
        )

    for path, new_value in body.form_data.items():
        cur = current_by_path.get(path)
        if cur is None:
            if new_value in (None, ""):
                continue
            answer = _human_answer(path, new_value)
            session.add(answer)
            await session.flush()
            session.add(
                DisputeAction(
                    tenant_id=tenant_id,
                    answer_id=answer.id,
                    actor_user_id=caller.user_id,
                    action=DisputeActionType.CORRECT.value,
                    old_value=None,
                    new_value={"value": new_value},
                )
            )
            changed.append(path)
            continue
        cur_value = unwrap_value(cur.value)
        if new_value != cur_value:
            cur.is_current = False
            await session.flush()  # clear the old current before inserting the new one
            session.add(_human_answer(path, new_value))
            session.add(
                DisputeAction(
                    tenant_id=tenant_id,
                    answer_id=cur.id,
                    actor_user_id=caller.user_id,
                    action=adjudication_action(new_value, cur_value, priors_by_path.get(path, [])),
                    old_value=cur.value,
                    new_value={"value": new_value},
                )
            )
            changed.append(path)
        elif path in dispute_fields:
            session.add(
                DisputeAction(
                    tenant_id=tenant_id,
                    answer_id=cur.id,
                    actor_user_id=caller.user_id,
                    action=DisputeActionType.ACCEPT.value,
                    old_value=cur.value,
                    new_value=cur.value,
                )
            )

    if body.reasked_fields:
        form.status = FormStatus.IN_QUEUE.value
        form.retry_count = form.retry_count + 1
    elif form.status == FormStatus.EXCEPTION_REVIEW.value:
        await session.flush()
        remaining = (await _unresolved_dispute_count_by_form(session, [form_id])).get(form_id, 0)
        if remaining == 0:
            form.status = FormStatus.COMPLETED.value

    await session.flush()
    current_paths = set(
        (
            await session.execute(
                select(FieldAnswer.field_path).where(
                    FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True)
                )
            )
        ).scalars()
    )
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    form.completion_pct = completion_pct(current_paths, version.schema_json)
    # Reload server-updated columns (e.g. updated_at onupdate) in-greenlet so the
    # detail build below doesn't trigger a sync lazy-load (MissingGreenlet).
    await session.refresh(form)

    detail = await _build_detail(session, form)
    audit = get_audit(request)
    # The response discloses every field value (PHI) — audit the disclosure, then the action.
    await audit.emit(
        _audit_phi_read(
            request, tenant_id, caller, str(form_id), [f.field_path for f in detail.fields]
        )
    )
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.DISPUTE_RESOLVE.value,
            resource_type="patient_form",
            resource_id=str(form_id),
            detail={
                "changed": changed,
                "accepted": sorted(dispute_fields - set(changed)),
                "reasked": body.reasked_fields,
            },
        )
    )
    return ok(detail, message="Disputes resolved.")
