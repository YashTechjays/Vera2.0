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

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased

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
    adjudication_action,
    build_field_views,
    completion_pct,
    unwrap_value,
)
from vera_core.models import (
    DisputeAction,
    FieldAnswer,
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
            appointment_type=promoted.appointment_type,
            member_policy_id=promoted.member_policy_id,
            insurance_provider=promoted.insurance_provider,
            insurance_provider_phone_number=promoted.insurance_provider_phone_number,
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
    """Worklist row — promoted identifiers + display columns."""

    id: UUID
    status: str
    patient_name: str | None
    chart_number: str | None
    appointment_date: date | None
    # Promoted out of intake_payload into typed columns (see PatientForm).
    appointment_type: str | None
    member_policy_id: str | None
    insurance_provider: str | None
    insurance_provider_phone_number: str | None
    completion_pct: float
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


def _baseline_subquery(form_id: UUID) -> Any:
    """Most recent `intake`/`human` answer per `field_path` for one form — the dispute
    baseline `B`. `created_at` is the transaction time, so same-transaction rows tie;
    `id DESC` (UUIDv7) breaks the tie deterministically."""
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
        .subquery()
    )


def _normalized_jsonb(expr: Any) -> Any:
    """A JSONB value canonicalized for dispute comparison: a JSON string is trimmed +
    lowercased (back into a jsonb string) so case/whitespace-only differences are not
    disputes; non-strings (numbers, bools, null, objects) are unchanged. Mirrors
    `vera_core.forms.review.normalize_value` — keep the two in lock-step, else the
    complete-gate count and the detail view disagree. (`btrim` with the explicit ASCII
    whitespace set matches Python `str.strip()` for ASCII; Unicode whitespace is a
    non-goal.)"""
    return case(
        (
            func.jsonb_typeof(expr) == "string",
            func.to_jsonb(func.lower(func.btrim(expr.astext, " \t\n\r\f\v"))),
        ),
        else_=expr,
    )


def _open_dispute_parts(form_id: UUID) -> tuple[Any, Any, Any, list[Any]]:
    """The single source of truth for "is this an open dispute": a current AI-call
    answer whose unwrapped value diverges from the baseline. Returns the pieces
    `(cur_alias, baseline_subquery, join_onclause, where_clauses)` so callers select
    whatever columns they need over the same FROM/JOIN/WHERE.

    Compares `value['value']` (the unwrapped raw) after normalization, so it matches
    `is_disputed`'s `normalize_value(unwrap_value(...))` comparison exactly; an absent
    baseline is `NULL`, so a divergent AI value with no prior counts as a dispute (per
    the confirmed rule)."""
    baseline = _baseline_subquery(form_id)
    cur = aliased(FieldAnswer)
    onclause = baseline.c.field_path == cur.field_path
    where = [
        cur.form_id == form_id,
        cur.is_current.is_(True),
        cur.source == AnswerSource.AI_CALL.value,
        _normalized_jsonb(cur.value["value"]).is_distinct_from(
            _normalized_jsonb(baseline.c.value["value"])
        ),
    ]
    return cur, baseline, onclause, where


async def _unresolved_dispute_count(session: TenantSession, form_id: UUID) -> int:
    """The number of current AI-call answers on this form whose value diverges from the
    intake/human baseline (the derived open-dispute count)."""
    cur, baseline, onclause, where = _open_dispute_parts(form_id)
    return (
        await session.execute(
            select(func.count()).select_from(cur).outerjoin(baseline, onclause).where(*where)
        )
    ).scalar_one()


async def _open_dispute_paths(session: TenantSession, form_id: UUID) -> set[str]:
    """The set of field paths with an open dispute on this form — used to gate which
    resolutions emit a `dispute_action` (audit record)."""
    cur, baseline, onclause, where = _open_dispute_parts(form_id)
    return set(
        (
            await session.execute(
                select(cur.field_path).select_from(cur).outerjoin(baseline, onclause).where(*where)
            )
        ).scalars()
    )


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

    # Baseline (most recent intake/human) per path → the dispute's `previous_value`.
    baseline = _baseline_subquery(form.id)
    baseline_by_path: dict[str, Any] = dict(
        (await session.execute(select(baseline.c.field_path, baseline.c.value))).tuples().all()
    )

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
        baseline_by_path,
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
    items = [
        PatientFormSummary(
            id=r.id,
            status=r.status,
            patient_name=r.patient_name,
            chart_number=r.chart_number,
            appointment_date=r.appointment_date,
            appointment_type=r.appointment_type,
            member_policy_id=r.member_policy_id,
            insurance_provider=r.insurance_provider,
            insurance_provider_phone_number=r.insurance_provider_phone_number,
            completion_pct=float(r.completion_pct),
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
                "member_policy_id",
                "insurance_provider",
                "insurance_provider_phone_number",
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
    # Lock the form row: resolve demotes the current answer and inserts a new one, so a
    # concurrent resolve (or the worker writing a fresh ai_call) would otherwise race to
    # two current rows and collide on the `fa_current_uq` partial unique index.
    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

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

    for path, new_value in body.form_data.items():
        cur = current_by_path.get(path)
        if cur is None:
            # No current answer to dispute — just record the human value (baseline edit).
            if new_value in (None, ""):
                continue
            session.add(_human_answer(path, new_value))
            changed.append(path)
            continue
        cur_value = unwrap_value(cur.value)
        if new_value != cur_value:
            # Value changed → advance the baseline with a human answer. Only record a
            # `dispute_action` when this path was actually disputed.
            cur.is_current = False
            await session.flush()  # clear the old current before inserting the new one
            session.add(_human_answer(path, new_value))
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
            cur.is_current = False
            await session.flush()
            session.add(_human_answer(path, cur_value))
            _record_action(cur.id, DisputeActionType.ACCEPT.value, cur.value, cur.value)
            accepted.append(path)

    # Status is NOT changed here. The lifecycle is driven only by the worker
    # (automatic path) and the dedicated PUT .../status endpoint (manual edges,
    # incl. EXCEPTION_REVIEW → COMPLETED once disputes are resolved). Adjudicating
    # disputes only records the human answers/actions; re-asked fields are surfaced
    # in the audit for the worker, and re-queueing is a manual status change.
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
                "accepted": sorted(accepted),
                "reasked": body.reasked_fields,
            },
        )
    )
    return ok(detail, message="Disputes resolved.")


# ---------------------------------------------------------------------------
# Manual status change (the single endpoint that mutates status by hand)
# ---------------------------------------------------------------------------

# Human-driven transitions the UI may request. The call pipeline owns the
# automatic core path (IN_QUEUE → IN_CALL → AI_PROCESSING → EXCEPTION_REVIEW) and
# the → CALL_FAILED edges; a reviewer/operator may only (re)queue work or complete
# a reviewed form. Any (current → target) pair absent here is rejected (422), so
# the worker-driven states can't be set by hand.
_ALLOWED_STATUS_TRANSITIONS: dict[FormStatus, frozenset[FormStatus]] = {
    FormStatus.READY_FOR_PROCESSING: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.CALL_FAILED: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.EXCEPTION_REVIEW: frozenset({FormStatus.IN_QUEUE, FormStatus.COMPLETED}),
}


class UpdateStatusRequest(BaseModel):
    status: FormStatus  # validated against the lifecycle enum (unknown value → 422)


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

    if target not in _ALLOWED_STATUS_TRANSITIONS.get(current, frozenset()):
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=f"cannot change status from '{current.value}' to '{target.value}'",
            data={"from": current.value, "to": target.value},
        )

    # A form may only complete once every judge-flagged dispute is adjudicated.
    if target == FormStatus.COMPLETED:
        remaining = await _unresolved_dispute_count(session, form_id)
        if remaining:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT,
                message="resolve all disputes before completing this form",
                data={"unresolved_disputes": remaining},
            )

    form.status = target.value
    await session.flush()

    # Status is not PHI — audit the state change (from/to) only; no PHI disclosure.
    await get_audit(request).emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.FORM_STATUS_CHANGE.value,
            resource_type="patient_form",
            resource_id=str(form_id),
            detail={"from": current.value, "to": target.value},
        )
    )
    return ok(
        PatientFormStatusResponse(id=form.id, status=form.status),
        message="Status updated.",
    )
