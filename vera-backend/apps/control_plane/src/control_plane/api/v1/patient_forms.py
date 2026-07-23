"""IBV patient-form endpoints (spec §4.3).

Two caller classes share this router:
- **Intake** (`POST /patient-forms`) — machine-to-machine, authenticated by an
  `intake:write` API key (tenant from the key, no user session): creates a
  `PatientForm` + `INTAKE`-source `field_answer` rows from a published schema version.
- **Display + dispute resolution + status** (`GET /patient-forms`, `GET /patient-forms/{id}`,
  `POST /patient-forms/{id}/disputes:resolve`, `PUT /patient-forms/{id}/status`) — the
  logged-in frontend user, on the session display-path chain (`require(...)` → tenant-scoped
  RLS session → PHI-access audit → `Cache-Control: no-store`). The status endpoint is the
  only manual mutator of `patient_form.status`; the worker drives the automatic lifecycle and
  `disputes:resolve` records adjudications without changing status.

Every PHI response audits field **names** only (never values).
"""

import asyncio
import hashlib
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from control_plane.api.v1.common import (
    AppSettings,
    CallPlans,
    Kms,
    LiveKit,
    TenantId,
    TenantSession,
    emit_phi_read_audit,
)
from control_plane.auth.api_key import ApiKeyPrincipal, require_scope
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, get_resolver, require
from control_plane.deps import get_audit, get_sessionmaker
from control_plane.dispatch import schedule_dispatch_pass
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.queueability import ensure_queueable
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord
from vera_core.db import tenant_session
from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.export import build_workbook
from vera_core.forms.intake import (
    InvalidIntakeValue,
    PromotedIdentifiers,
    date_leaf_paths,
    iter_leaf_answers,
    missing_required,
    normalize_date_answers,
    normalize_date_value,
    normalize_phone_answers,
    normalize_phone_prefix,
    phone_promoted_paths,
    promote_columns,
    unknown_payload_paths,
)
from vera_core.forms.review import (
    AnswerRow,
    adjudication_action,
    build_field_views,
    completion_pct,
    completion_pct_v2,
    normalize_value,
    unwrap_value,
)
from vera_core.models import (
    DisputeAction,
    ExportArtifact,
    FieldAnswer,
    FormSchema,
    InsuranceProvider,
    PatientForm,
    SchemaVersion,
    Tenant,
)
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import (
    AnswerSource,
    DisputeActionType,
    FormStatus,
    ProviderStatus,
)
from vera_core.services.call_provenance import (
    CallAttempt,
    FieldProvenance,
    load_call_attempts,
    load_field_provenance,
)
from vera_core.services.call_visibility import call_hidden_from
from vera_core.services.field_answers import current_values_by_path
from vera_core.services.field_status import load_field_status
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.recordings import recording_config_from

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


def _v2_doc(schema_json: dict[str, Any]) -> FormSchemaDoc | None:
    """The parsed v2 document, or `None` for a legacy v1 schema — the single "is this
    v2, and if so hand me the doc" check shared by intake and dispute-resolve column
    promotion (both call `promote_columns` against it)."""
    return FormSchemaDoc.model_validate(schema_json) if is_v2(schema_json) else None


def _raise_422(exc: InvalidIntakeValue) -> NoReturn:
    raise CustomAPIException(
        DefaultExceptionCode.VALIDATION_ERROR,
        message="invalid field value",
        data={"fields": [exc.field_path]},
    ) from exc


def _promote_or_422(get_value: Callable[[str], Any], doc: FormSchemaDoc) -> PromotedIdentifiers:
    """`promote_columns`, translated to the API's validation-error contract — the
    error-wrapping shared by intake and dispute-resolve column promotion."""
    try:
        return promote_columns(get_value, doc)
    except InvalidIntakeValue as exc:
        _raise_422(exc)


def _normalize_date_answers_or_422(
    answers: list[tuple[str, Any]], doc: FormSchemaDoc
) -> list[tuple[str, Any]]:
    """`normalize_date_answers`, translated to the API's validation-error
    contract — every date-typed leaf's intake value gets reformatted to its
    declared `date_format`, not just the promoted `patient_dob`/
    `appointment_date` columns `_promote_or_422` covers."""
    try:
        return normalize_date_answers(answers, doc)
    except InvalidIntakeValue as exc:
        _raise_422(exc)


def _normalize_date_value_or_422(value: Any, field_path: str, date_format: str | None) -> Any:
    """`normalize_date_value`, translated to the API's validation-error contract —
    the single-leaf counterpart to `_normalize_date_answers_or_422`, used when a
    dispute-resolve edit reformats one date leaf's answer to its declared format."""
    try:
        return normalize_date_value(value, field_path, date_format)
    except InvalidIntakeValue as exc:
        _raise_422(exc)


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
        doc = _v2_doc(version.schema_json)

        # Flattened + phone-normalized intake answers: one INTAKE-source field_answer per
        # provided leaf. v2 documents use root-anchored paths (`sections.…` — spec §4.2), so
        # the payload (nested by section_key) is flattened under a `sections` root. v1
        # schemas have no leaf set to validate against, so the unknown-path check and phone
        # normalization both live in the `doc is not None` branch. Building `answers` before
        # promotion (rather than after) lets `promote_columns` read the already-`+`-prefixed
        # value, so field_answer and the promoted column agree on it (2026-07-15 design doc).
        if doc is not None:
            answers = list(iter_leaf_answers({"sections": body.intake_payload}))
            unrecognized = unknown_payload_paths(answers, doc)
            if unrecognized:
                raise CustomAPIException(
                    DefaultExceptionCode.VALIDATION_ERROR,
                    message="intake payload contains unknown field paths",
                    data={"fields": unrecognized},
                )
            answers = normalize_phone_answers(answers, doc)
            answers = _normalize_date_answers_or_422(answers, doc)
            promoted = _promote_or_422(dict(answers).get, doc)
        else:
            answers = list(iter_leaf_answers(body.intake_payload))
            promoted = PromotedIdentifiers()

        form = PatientForm(
            tenant_id=principal.tenant_id,
            schema_version_id=body.schema_version_id,
            status=FormStatus.READY_FOR_PROCESSING.value,
            intake_payload=body.intake_payload,
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
    """Worklist row — promoted identifiers + display columns."""

    id: UUID
    status: str
    patient_name: str | None
    chart_number: str | None
    appointment_date: date | None
    # Promoted out of intake_payload into typed columns (see PatientForm).
    appointment_type: str | None
    member_id: str | None
    insurance_provider: str | None
    insurance_provider_phone_number: str | None
    completion_pct: float
    review_reason: str | None
    created_at: datetime
    updated_at: datetime


class PaginatedForms(BaseModel):
    items: list[PatientFormSummary]
    page: int
    page_size: int
    total: int


SortKey = Literal[
    "appointment_date",
    "appointment_type",
    "patient_name",
    "member_id",
    "insurance_provider",
    "status",
    "created_at",
]
_SORT_COLUMNS = {
    "appointment_date": PatientForm.appointment_date,
    "appointment_type": PatientForm.appointment_type,
    "patient_name": PatientForm.patient_name,
    "member_id": PatientForm.member_id,
    "insurance_provider": PatientForm.insurance_provider,
    "status": PatientForm.status,
    "created_at": PatientForm.created_at,
}


class DisputeView(BaseModel):
    previous_value: Any
    current_value: Any
    confidence: int | None
    evidence: str | None
    reasoning: str | None


class JudgeView(BaseModel):
    confidence: int | None
    supported: bool
    evidence: str | None


class ProvenanceView(BaseModel):
    attempt: int
    mode: str
    judge: JudgeView | None


class FieldView(BaseModel):
    field_path: str
    value: Any
    source: str
    confidence: int | None
    dispute: DisputeView | None
    provenance: ProvenanceView | None = None


def _provenance_view(p: FieldProvenance | None) -> ProvenanceView | None:
    # Explicit field mapping: the view models ARE the API contract, so a service-
    # dataclass rename or new field must be an explicit decision here — not a
    # silent splat-through (or runtime TypeError) via dataclasses.asdict.
    if p is None:
        return None
    judge = (
        JudgeView(
            confidence=p.judge.confidence, supported=p.judge.supported, evidence=p.judge.evidence
        )
        if p.judge is not None
        else None
    )
    return ProvenanceView(attempt=p.attempt, mode=p.mode, judge=judge)


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
    # The form's current insurance provider (promoted intake column). The send-to-queue
    # UI pre-selects the matching catalog provider from this string.
    insurance_provider: str | None
    # Voice-lab-style toggle stored on the form (default True) — the UI's re-queue
    # toggle pre-loads from here so an operator's earlier choice round-trips.
    ivr_navigation_enabled: bool
    fields: list[FieldView]


class CallAttemptView(BaseModel):
    id: UUID
    attempt: int
    mode: str
    status: str
    created_at: datetime
    retry_of: UUID | None
    changed_paths: list[str]
    # True only when THIS caller may actually fetch the recording: it is
    # AVAILABLE, the call passes the playback endpoint's owner-or-published
    # gate, and the caller holds recordings:read — the DTO must never
    # advertise a recording the playback endpoint would refuse.
    recording_available: bool


def _call_attempt_view(a: CallAttempt, caller_id: UUID | None, can_play: bool) -> CallAttemptView:
    visible = not call_hidden_from(a.initiated_by_id, a.published, caller_id)
    return CallAttemptView(
        id=a.id,
        attempt=a.attempt,
        mode=a.mode,
        status=a.status,
        created_at=a.created_at,
        retry_of=a.retry_of,
        changed_paths=a.changed_paths,
        recording_available=a.recording_available and visible and can_play,
    )


class ResolveRequest(BaseModel):
    form_data: dict[str, Any] = {}  # current value per dotted path (post-edit)
    dispute_fields: list[str] = []  # paths the reviewer explicitly accepted
    reasked_fields: list[str] = []  # paths to re-verify on the next call


def _baseline_query(form_id: UUID) -> Any:
    """`(field_path, value)` of the most recent `intake`/`human` answer per `field_path`
    for one form — the dispute baseline `B`. `created_at` is the transaction time, so
    same-transaction rows tie; `id DESC` (UUIDv7) breaks the tie deterministically."""
    return (
        select(FieldAnswer.field_path, FieldAnswer.value)
        .where(
            FieldAnswer.form_id == form_id,
            FieldAnswer.source.in_([AnswerSource.INTAKE.value, AnswerSource.HUMAN.value]),
        )
        .order_by(
            FieldAnswer.field_path,
            FieldAnswer.created_at.desc(),
            FieldAnswer.id.desc(),
        )
        .distinct(FieldAnswer.field_path)
    )


async def _field_views(session: TenantSession, form_id: UUID) -> list[dict[str, Any]]:
    """The dispute-annotated field views for one form — the single source of truth for
    "is this a dispute". Both the detail view and the dispute gate go through here, so the
    count and the detail can never disagree. The dispute decision (incl. value
    normalization and null handling) lives once, in `build_field_views`/`is_disputed`."""
    current = (
        (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    baseline_by_path: dict[str, Any] = dict(
        (await session.execute(_baseline_query(form_id))).tuples().all()
    )
    return build_field_views(
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
        baseline_by_path,
    )


async def _open_dispute_paths(session: TenantSession, form_id: UUID) -> set[str]:
    """The set of field paths with an open dispute on this form — used to gate which
    resolutions emit a `dispute_action` (audit record)."""
    return {
        v["field_path"] for v in await _field_views(session, form_id) if v["dispute"] is not None
    }


async def _unresolved_dispute_count(session: TenantSession, form_id: UUID) -> int:
    """The number of fields on this form with an open dispute (derived from the same
    Python rule the detail view uses)."""
    return len(await _open_dispute_paths(session, form_id))


async def _build_detail(session: TenantSession, form: PatientForm) -> PatientFormDetail:
    """Assemble the full review detail for one form (current answers + disputes)."""
    views = await _field_views(session, form.id)

    form_schema = (
        await session.execute(
            select(FormSchema)
            .join(SchemaVersion, SchemaVersion.schema_id == FormSchema.id)
            .where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()

    attempts = await load_call_attempts(session, form.id)
    # No calls → no ai_call answers → nothing to join; skip the provenance query
    # (this runs on every form-detail GET, incl. intake-only forms).
    prov = (
        await load_field_provenance(session, form.id, {a.id: (a.attempt, a.mode) for a in attempts})
        if attempts
        else {}
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
        insurance_provider=form.insurance_provider,
        ivr_navigation_enabled=form.ivr_navigation_enabled,
        fields=[
            FieldView(**view, provenance=_provenance_view(prov.get(view["field_path"])))
            for view in views
        ],
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
    sort_by: SortKey = "created_at",
    sort_dir: Literal["asc", "desc"] = "desc",
    caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[PaginatedForms]:
    response.headers["Cache-Control"] = "no-store"
    conds = []
    if status is not None:
        conds.append(PatientForm.status == status)
    if q:
        conds.append(PatientForm.patient_name.ilike(f"%{q.lower()}%"))
    sort_col = _SORT_COLUMNS[sort_by]
    primary = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

    async def _fetch_page() -> tuple[list[PatientForm], int]:
        """One round trip: the page rows with the filtered total as a window
        column. An out-of-range page returns no rows (so no total); fall back
        to a bare count for it."""
        result = (
            await session.execute(
                select(PatientForm, func.count().over())
                .where(*conds)
                # created_at tie-break keeps pages stable when the sort key repeats.
                .order_by(primary.nulls_last(), PatientForm.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        if result:
            return [form for form, _total in result], result[0][1]
        total = (
            await session.execute(select(func.count()).select_from(PatientForm).where(*conds))
        ).scalar_one()
        return [], total

    audited_fields = [
        "patient_name",
        "chart_number",
        "appointment_date",
        "appointment_type",
        "member_id",
        "insurance_provider",
        "insurance_provider_phone_number",
    ]
    # The PHI-access audit writes in its own session/transaction, so it can
    # overlap the page query instead of serializing after it; it is still
    # awaited before any data leaves (audit-before-disclosure).
    # return_exceptions=True: a plain gather() would, on one coroutine raising,
    # propagate immediately while leaving the other running in the background —
    # here that would mean _fetch_page still executing against `session` after
    # this request's teardown starts closing it. Collecting both results first
    # and raising explicitly keeps them from outliving this function.
    fetch_result, audit_result = await asyncio.gather(
        _fetch_page(),
        emit_phi_read_audit(
            get_audit(request),
            request,
            tenant_id=tenant_id,
            caller=caller,
            resource_type="patient_form",
            resource_id="list",
            fields=audited_fields,
        ),
        return_exceptions=True,
    )
    if isinstance(audit_result, BaseException):
        raise audit_result
    if isinstance(fetch_result, BaseException):
        raise fetch_result
    rows, total = fetch_result
    items = [
        PatientFormSummary(
            id=r.id,
            status=r.status,
            patient_name=r.patient_name,
            chart_number=r.chart_number,
            appointment_date=r.appointment_date,
            appointment_type=r.appointment_type,
            member_id=r.member_id,
            insurance_provider=r.insurance_provider,
            insurance_provider_phone_number=r.insurance_provider_phone_number,
            completion_pct=float(r.completion_pct),
            review_reason=r.review_reason,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
    return ok(PaginatedForms(items=items, page=page, page_size=page_size, total=total))


class ProviderOption(BaseModel):
    """Minimal active-provider option for the send-to-queue provider picker (non-PHI)."""

    id: UUID
    name: str


# Declared BEFORE `/patient-forms/{form_id}` so the literal path is matched instead of
# being captured as a (non-UUID) form_id and 422'd.
@router.get(
    "/patient-forms/insurance-providers",
    response_model=ResponseModel[list[ProviderOption]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_form_insurance_providers(
    response: Response,
    session: TenantSession,
    _caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[list[ProviderOption]]:
    """Active insurance providers an operator can pick when sending a form to the queue. The
    insurance_provider catalog is GLOBAL (no RLS, no PHI), so it resolves on the tenant session."""
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.execute(
            select(InsuranceProvider.id, InsuranceProvider.name)
            .where(InsuranceProvider.status == ProviderStatus.ACTIVE)
            .order_by(InsuranceProvider.name)
        )
    ).all()
    return ok([ProviderOption(id=row.id, name=row.name) for row in rows])


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
    await emit_phi_read_audit(
        get_audit(request),
        request,
        tenant_id=tenant_id,
        caller=caller,
        resource_type="patient_form",
        resource_id=str(form_id),
        fields=[f.field_path for f in detail.fields],
    )
    return ok(detail)


@router.get(
    "/patient-forms/{form_id}/calls",
    response_model=ResponseModel[list[CallAttemptView]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_form_calls(
    form_id: UUID,
    request: Request,
    response: Response,
    session: TenantSession,
    tenant_id: TenantId,
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[list[CallAttemptView]]:
    """The form's call-attempt timeline: mode, status, lineage, and which field
    paths each call changed. Paths and timings only — no field values."""
    response.headers["Cache-Control"] = "no-store"
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")
    attempts = await load_call_attempts(session, form_id)
    await emit_phi_read_audit(
        get_audit(request),
        request,
        tenant_id=tenant_id,
        caller=caller,
        resource_type="patient_form",
        resource_id=str(form_id),
        fields=sorted({p for a in attempts for p in a.changed_paths}),
    )
    # recordings:read shapes recording_available only (no 403 — the timeline
    # itself needs just forms:read); the playback endpoint re-enforces it.
    _, permissions = await resolver.effective_permissions(session, tenant_id, caller.user_id)
    can_play = "recordings:read" in permissions
    return ok([_call_attempt_view(a, caller.user_id, can_play) for a in attempts])


@router.post(
    "/patient-forms/{form_id}/disputes:resolve",
    response_model=ResponseModel[PatientFormDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
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
    # Lock the form row to serialize concurrent resolves: resolve demotes the current answer
    # and inserts a new one, so two overlapping resolves would otherwise race to two current
    # rows. (This lock does NOT cover a worker writing a fresh ai_call — the worker doesn't
    # take it; that case relies on the `fa_current_uq` partial unique index.)
    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    # Fetched here (not after the edit loop, as before) so phone-typed promoted paths
    # are known before normalizing incoming edits below (2026-07-15 design doc).
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    doc = _v2_doc(version.schema_json)
    phone_paths = phone_promoted_paths(doc) if doc is not None else set()
    date_paths = date_leaf_paths(doc) if doc is not None else {}

    # Open disputes BEFORE any writes: only an actually-disputed path may emit a
    # `dispute_action` (a pre-call/baseline edit advances the baseline without one).
    open_paths = await _open_dispute_paths(session, form_id)

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
    accepted: list[str] = []

    def _human_answer(path: str, raw: Any) -> FieldAnswer:
        return FieldAnswer(
            tenant_id=tenant_id,
            form_id=form_id,
            field_path=path,
            value={"value": raw},
            source=AnswerSource.HUMAN.value,
            is_current=True,
        )

    def _record_action(answer_id: UUID, action: str, old: Any, new: Any) -> None:
        session.add(
            DisputeAction(
                tenant_id=tenant_id,
                answer_id=answer_id,
                actor_user_id=caller.user_id,
                action=action,
                old_value=old,
                new_value=new,
            )
        )

    async def _supersede(cur: FieldAnswer, raw: Any) -> None:
        # Demote the current answer and write a HUMAN checkpoint (advances the baseline).
        cur.is_current = False
        await session.flush()  # clear the old current before inserting the new one
        session.add(_human_answer(cur.field_path, raw))

    for path, new_value in body.form_data.items():
        if path in phone_paths:
            new_value = normalize_phone_prefix(new_value)
        if path in date_paths:
            new_value = _normalize_date_value_or_422(new_value, path, date_paths[path])
        cur = current_by_path.get(path)
        if cur is None:
            # No current answer to dispute — just record the human value (baseline edit).
            if new_value in (None, ""):
                continue
            session.add(_human_answer(path, new_value))
            changed.append(path)
            continue
        cur_value = unwrap_value(cur.value)
        if normalize_value(new_value) != normalize_value(cur_value):
            # Real change (differs under the dispute rule) → advance the baseline with a
            # human answer. Only record a `dispute_action` when this path was disputed.
            await _supersede(cur, new_value)
            if path in open_paths:
                _record_action(
                    cur.id,
                    adjudication_action(new_value, cur_value, priors_by_path.get(path, [])),
                    cur.value,
                    {"value": new_value},
                )
            changed.append(path)
        elif path in dispute_fields and path in open_paths:
            # Accept the AI value as-is: write a HUMAN checkpoint equal to it so the
            # baseline advances and the dispute clears, then record the adjudication.
            await _supersede(cur, cur_value)
            _record_action(cur.id, DisputeActionType.ACCEPT.value, cur.value, cur.value)
            accepted.append(path)

    # Status is NOT changed here. The lifecycle is driven only by the worker
    # (automatic path) and the dedicated PUT .../status endpoint (manual edges,
    # incl. EXCEPTION_REVIEW → COMPLETED once disputes are resolved). Adjudicating
    # disputes only records the human answers/actions; re-asked fields are surfaced
    # in the audit for the worker, and re-queueing is a manual status change.
    await session.flush()
    current_values: dict[str, Any] = await current_values_by_path(session, form_id)
    # doc/phone_paths already resolved above. Re-derive promoted patient_form columns
    # from the post-write current answers — any resolve call that changes a promoted
    # field's value (dispute or plain edit) keeps the worklist columns in sync, not
    # just intake (2026-07-10 design doc).
    if doc is not None:
        promoted = _promote_or_422(current_values.get, doc)
        for column, _path in doc.promoted_fields.items():
            new_value = getattr(promoted, column)
            if getattr(form, column) != new_value:
                setattr(form, column, new_value)
    # v2 completion needs the values (applicable_when/required.when evaluate against
    # them); v1 only needs which paths are filled.
    form.completion_pct = (
        completion_pct_v2(current_values, version.schema_json)
        if doc is not None
        else completion_pct(set(current_values), version.schema_json)
    )
    # Flush BEFORE refresh: refresh() reloads from the DB and DISCARDS pending
    # attribute changes — without this flush the completion update was silently
    # lost. The refresh then reloads server-updated columns (updated_at onupdate)
    # in-greenlet so the detail build below doesn't sync-lazy-load (MissingGreenlet).
    await session.flush()
    await session.refresh(form)

    detail = await _build_detail(session, form)
    audit = get_audit(request)
    # The response discloses every field value (PHI) — audit the disclosure, then the action.
    await emit_phi_read_audit(
        audit,
        request,
        tenant_id=tenant_id,
        caller=caller,
        resource_type="patient_form",
        resource_id=str(form_id),
        fields=[f.field_path for f in detail.fields],
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
                "accepted": sorted(accepted),
                "reasked": body.reasked_fields,
            },
        )
    )
    return ok(detail, message="Disputes resolved.")


# ---------------------------------------------------------------------------
# XLSX export (binary — errors still ride the standard envelope)
# ---------------------------------------------------------------------------

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post(
    "/patient-forms/{form_id}/export",
    response_class=Response,
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def export_patient_form(
    form_id: UUID,
    request: Request,
    session: TenantSession,
    tenant_id: TenantId,
    caller: VerifiedIdentity = require("forms:export"),
) -> Response:
    """Stream the COMPLETED form as XLSX — a PHI disclosure. Writes one
    export_artifact ledger row + a FORM_EXPORTED audit (field names only).
    The one binary endpoint: errors still ride the standard envelope."""
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")
    if form.status != FormStatus.COMPLETED:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="only completed forms can be exported",
            data={"status": form.status},
        )
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    values = await current_values_by_path(session, form_id)
    sources = {p: s.source or "" for p, s in (await load_field_status(session, form_id)).items()}
    attempts = await load_call_attempts(session, form_id)
    prov = await load_field_provenance(
        session, form_id, {a.id: (a.attempt, a.mode) for a in attempts}
    )
    data = build_workbook(version.schema_json, values, sources, prov, attempts)

    artifact = ExportArtifact(
        tenant_id=tenant_id,
        form_id=form_id,
        format="xlsx",
        sha256=hashlib.sha256(data).hexdigest(),
        exported_by=caller.user_id,
    )
    session.add(artifact)
    await session.flush()
    await get_audit(request).emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.FORM_EXPORTED.value,
            resource_type="patient_form",
            resource_id=str(form_id),
            detail={"artifact_id": str(artifact.id), "format": "xlsx", "fields": sorted(values)},
        )
    )
    return Response(
        content=data,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="ibv-{form_id}.xlsx"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Schema version lookup (the document a form is pinned to)
# ---------------------------------------------------------------------------


class SchemaVersionDetail(BaseModel):
    """One stored schema document. Global catalog, not PHI — the form template,
    never patient values."""

    id: UUID
    schema_id: UUID
    version: int
    status: str
    insurance_type: str
    name: str
    # The stored schema_version.schema_json ("schema_json" itself collides with a
    # pydantic BaseModel method name).
    document: dict[str, Any]


@router.get(
    "/schema-versions/{version_id}",
    response_model=ResponseModel[SchemaVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_schema_version(
    version_id: UUID,
    response: Response,
    session: TenantSession,
    caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[SchemaVersionDetail]:
    """The schema document a patient form is bound to via `schema_version_id` —
    the frontend fetches this to render the form (never a bundled copy). Reads
    the global catalog only (no patient data), so no PHI-access audit."""
    response.headers["Cache-Control"] = "no-store"
    row = (
        await session.execute(
            select(SchemaVersion, FormSchema)
            .join(FormSchema, FormSchema.id == SchemaVersion.schema_id)
            .where(SchemaVersion.id == version_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError(message="schema version not found")
    version, form_schema = row
    return ok(
        SchemaVersionDetail(
            id=version.id,
            schema_id=version.schema_id,
            version=version.version,
            status=version.status,
            insurance_type=form_schema.insurance_type,
            name=form_schema.name,
            document=version.schema_json,
        )
    )


# ---------------------------------------------------------------------------
# Manual status change (the single endpoint that mutates status by hand)
# ---------------------------------------------------------------------------

# Human-driven transitions the UI may request. The call pipeline owns the
# automatic core path (IN_QUEUE → IN_CALL → AI_PROCESSING → EXCEPTION_REVIEW) and
# the → CALL_FAILED edges; a reviewer/operator may only (re)queue work or complete
# a reviewed form. Any (current → target) pair absent here is rejected (422), so
# the worker-driven states can't be set by hand.
_MANUAL_TARGETS: dict[FormStatus, frozenset[FormStatus]] = {
    FormStatus.READY_FOR_PROCESSING: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.CALL_FAILED: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.EXCEPTION_REVIEW: frozenset({FormStatus.IN_QUEUE, FormStatus.COMPLETED}),
}


class UpdateStatusRequest(BaseModel):
    status: FormStatus  # validated against the lifecycle enum (unknown value → 422)
    # Voice-lab-style toggle, meaningful only on → IN_QUEUE: should the dispatched
    # call run the IVR navigator? None keeps the form's stored choice (so a requeue
    # without the field preserves the operator's earlier decision).
    enable_ivr_navigation: bool | None = None
    # Operator-picked insurance provider, meaningful only on → IN_QUEUE. The form's
    # `insurance_provider` string is canonicalized to this catalog provider's exact
    # name so the async dispatcher resolves the right provider (and its IVR playbook).
    # None leaves the intake string untouched (dispatch falls back to its own match).
    insurance_provider_id: UUID | None = None


class PatientFormStatusResponse(BaseModel):
    """Non-PHI acknowledgement of a status change."""

    id: UUID
    status: str


@router.put(
    "/patient-forms/{form_id}/status",
    response_model=ResponseModel[PatientFormStatusResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def update_patient_form_status(
    form_id: UUID,
    body: UpdateStatusRequest,
    request: Request,
    response: Response,
    session: TenantSession,
    tenant_id: TenantId,
    livekit: LiveKit,
    kms: Kms,
    settings: AppSettings,
    call_plans: CallPlans,
    caller: VerifiedIdentity = require("forms:write"),
) -> ResponseModel[PatientFormStatusResponse]:
    """Change a patient form's lifecycle status — the only endpoint that mutates
    `status` by hand. Status-only: no other field is touched. Enforces the manual
    transition state machine, and blocks → COMPLETED while disputes are unresolved."""
    response.headers["Cache-Control"] = "no-store"
    # Lock the row: `status` is driven by both this endpoint and the worker, so the
    # read → validate → write must serialize against a concurrent transition,
    # otherwise two changes (e.g. two reviewers, or reviewer vs. worker) race to a
    # lost update and the loser's edge is never re-validated.
    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    current = FormStatus(form.status)
    target = body.status

    # Idempotent no-op: nothing to change, validate, or audit.
    if target == current:
        return ok(
            PatientFormStatusResponse(id=form.id, status=form.status),
            message="Status unchanged.",
        )

    # Manual-endpoint guard: only the transitions in _MANUAL_TARGETS are allowed here.
    if target not in _MANUAL_TARGETS.get(current, frozenset()):
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=f"cannot change status from '{current.value}' to '{target.value}'",
            data={"from": current.value, "to": target.value},
        )

    # Hard dialability gate: a form that can never be dialed must not enter the queue.
    canonicalized_provider = False
    if target == FormStatus.IN_QUEUE:
        await ensure_queueable(session, kms, form)
        # Canonicalize the provider from the operator's pick so the async dispatcher
        # resolves the right catalog provider (and its IVR playbook) — the string, not
        # a new FK, carries the choice. insurance_provider is GLOBAL (no RLS).
        if body.insurance_provider_id is not None:
            provider = (
                await session.execute(
                    select(InsuranceProvider).where(
                        InsuranceProvider.id == body.insurance_provider_id,
                        InsuranceProvider.status == ProviderStatus.ACTIVE,
                    )
                )
            ).scalar_one_or_none()
            if provider is None:
                raise CustomAPIException(
                    DefaultExceptionCode.VALIDATION_ERROR,
                    message="unknown or inactive insurance provider",
                    data={"field": "insurance_provider_id"},
                )
            form.insurance_provider = provider.name
            canonicalized_provider = True

    # A form may only complete once every judge-flagged dispute is adjudicated.
    if target == FormStatus.COMPLETED:
        remaining = await _unresolved_dispute_count(session, form_id)
        if remaining:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT,
                message="resolve all disputes before completing this form",
                data={"unresolved_disputes": remaining},
            )

    # Load tenant for state machine guard (retry cap).
    tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()

    sm = FormStateMachine()
    try:
        # This endpoint is the operator surface — manual transitions start a
        # fresh enqueue episode (never blocked by, and resetting, the retry cap).
        sm.transition(form, target, tenant_max_retries=tenant.max_retries, manual=True)
    except InvalidTransitionError as exc:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=str(exc),
            data={"from": current.value, "to": target.value},
        ) from exc

    # review_reason is stamped/cleared inside FormStateMachine.transition.

    # Callers own enqueued_at — use the DB clock to avoid cross-node skew.
    if target == FormStatus.IN_QUEUE:
        form.enqueued_at = func.now()
        # Persist the queuer so the dispatcher can attribute call ownership
        # (`initiated_by_id`) even when the call is created later by a different
        # actor (freed-slot dispatch, retry-at-callback).
        form.enqueued_by_id = caller.user_id
        if body.enable_ivr_navigation is not None:
            form.ivr_navigation_enabled = body.enable_ivr_navigation

    await session.flush()

    # Status is not PHI — audit the state change (from/to) only; no PHI disclosure.
    detail: dict[str, Any] = {"from": current.value, "to": target.value}
    if target == FormStatus.IN_QUEUE:
        detail["ivr_navigation"] = form.ivr_navigation_enabled
        # Record the mutated field NAME only (never the value) per the audit contract.
        if canonicalized_provider:
            detail["fields"] = ["insurance_provider"]

    audit = get_audit(request)
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.FORM_STATUS_CHANGE.value,
            resource_type="patient_form",
            resource_id=str(form_id),
            detail=detail,
        )
    )

    # Kick a dispatch pass strictly AFTER this transaction commits: a detached
    # task whose first statement lock-waits on the enqueued row (see
    # control_plane.dispatch). NOT fastapi.BackgroundTasks — those run before
    # yield-dependency teardown, i.e. before this transaction's commit. The
    # response acknowledges the manual transition only; clients observe dispatch
    # via the calls list.
    if target == FormStatus.IN_QUEUE:
        schedule_dispatch_pass(
            get_sessionmaker(request),
            tenant_id,
            livekit,
            kms,
            audit,
            wait_for_form_id=form_id,
            recording=recording_config_from(settings),
            plan_service=call_plans,
        )

    return ok(
        PatientFormStatusResponse(id=form.id, status=target.value),
        message="Status updated.",
    )
