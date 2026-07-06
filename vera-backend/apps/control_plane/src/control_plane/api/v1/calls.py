"""Verification-call endpoints: create, join-token, active-list.

Auth note (acknowledged stopgap): all three endpoints guard with
`require("calls:read")` for now — the SPA has no real auth yet, and the
spec flags this.  A later task tightens POST / join-token to a write /
manage permission once the RBAC catalog is extended.
"""

import contextlib
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import CallPlans, LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import get_audit
from control_plane.exceptions import (
    ConflictError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.ivr_selection import add_active_playbook_metadata
from control_plane.request_context import current_request_id
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord, AuditSink
from vera_core.callplan import (
    CallPlanStore,
    CompileError,
    build_prefill,
    compile_call_plan,
)
from vera_core.forms.intake import iter_leaf_answers
from vera_core.forms.review import unwrap_value
from vera_core.models import Call, CallEvent, FieldAnswer, InsuranceProvider, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.authoring import Prompt, PromptVersion, SchemaVersion
from vera_core.models.enums import (
    CallEventType,
    CallStatus,
    FormStatus,
    ProviderStatus,
    VersionStatus,
)
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import CallSummary, JoinTokenResponse, PersonaTweak, StartCallRequest
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.queue_dispatcher import try_dispatch

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calls"])

_ACTIVE_STATUSES = (
    CallStatus.INITIATED,
    CallStatus.RINGING,
    CallStatus.IVR,
    CallStatus.ACTIVE,
    CallStatus.WAITING,
    CallStatus.CRITICAL,
)


def _summary(call: Call, patient_name: str | None) -> CallSummary:
    return CallSummary(
        id=call.id,
        tenant_id=call.tenant_id,
        status=call.current_status,
        room_name=room_name_for_call(call.tenant_id, call.id),
        patient_name=patient_name,
        started_at=call.started_at,
        created_at=call.created_at,
    )


@router.post(
    "/calls",
    response_model=ResponseModel[CallSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def start_call(
    body: StartCallRequest,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    call_plans: CallPlans,
    request: Request,
    caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
    audit: AuditSink = Depends(get_audit),
) -> ResponseModel[CallSummary]:
    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == body.form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")
    if body.insurance_provider_id is not None:
        # Require an ACTIVE provider here: an unknown id would otherwise FK-violate → 500 at
        # flush, and an inactive provider must not start a call (nor let its playbook steer one).
        provider_active = (
            await session.execute(
                select(InsuranceProvider.id).where(
                    InsuranceProvider.id == body.insurance_provider_id,
                    InsuranceProvider.status == ProviderStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        if provider_active is None:
            raise NotFoundError(message="unknown or inactive insurance provider")

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()  # RLS on `tenant` keys on id → only the caller's own row

    # A manual call takes the form to IN_CALL through the state machine BEFORE
    # the Call/room exist: the status callback then has a legal edge to a
    # terminal status, the dispatcher won't treat the form as queue-eligible
    # (no double dispatch), and the form counts against the tenant's concurrency
    # slots. The IN_QUEUE hop is a synthetic pass-through (no enqueued_at, never
    # dispatcher-visible — both transitions commit atomically) and a no-op when
    # the form is already queued. Illegal states (e.g. already IN_CALL or
    # COMPLETED) are rejected here instead of creating a stray call.
    sm = FormStateMachine()
    try:
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant.max_retries)
        sm.transition(form, FormStatus.IN_CALL, tenant_max_retries=tenant.max_retries)
    except InvalidTransitionError as exc:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=f"cannot start a call for this form: {exc}",
        ) from exc

    call = Call(
        tenant_id=tenant_id,
        form_id=form.id,
        current_status=CallStatus.INITIATED,
        insurance_provider_id=body.insurance_provider_id,
    )
    session.add(call)
    await session.flush()  # populates call.id (UUIDv7)

    room_name = room_name_for_call(tenant_id, call.id)
    # persona_tweak is admin-authored, non-PHI config; safe to serialize into metadata.
    # Nested under its own key so sibling dispatch keys never trip the worker's
    # extra="forbid" PersonaTweak validation (see agent_worker.prompt.parse_persona_tweak).
    tweak = (
        PersonaTweak.model_validate(tenant.persona_tweak)
        if tenant.persona_tweak
        else PersonaTweak()
    )
    metadata: dict[str, object] = {}
    if tweak_fields := tweak.model_dump(exclude_none=True):
        metadata["persona_tweak"] = tweak_fields
    # When navigating the payer IVR, specialize the navigator with the provider's active playbook
    # (non-PHI overlay) if one exists; otherwise it runs generic. Off preserves today's behavior.
    if body.enable_ivr_navigation:
        metadata["enable_ivr_navigation"] = True
        await add_active_playbook_metadata(session, body.insurance_provider_id, metadata)

    # Compile the per-call plan (schema + DB prefill → runtime artifact) and stash
    # it BEFORE the room/dispatch exists, so the worker can never race an absent
    # plan. Fail-safe: a schema that cannot compile stashes nothing (disclosed=None)
    # and the worker runs its static-persona fallback.
    disclosed = await _compile_and_stash(session, call_plans, call, form, tweak, room_name)
    if disclosed is not None:
        # Prefilling known values into the plan for the worker is a disclosure:
        # audit the field NAMES (never values) — the exact set of DB values
        # compiled into the plan, computed at the disclosure point.
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_user_id=caller.user_id,
                actor_label=caller.email or caller.subject,
                event_type=AuditEvent.PHI_ACCESS.value,
                resource_type="patient_form",
                resource_id=str(form.id),
                permission_key="calls:read",
                decision="allow",
                request_id=current_request_id(request),
                detail={"fields": disclosed},
            )
        )

    await livekit.create_call_room(room_name, metadata=metadata)
    session.add(
        CallEvent(
            tenant_id=tenant_id,
            call_id=call.id,
            event_type=CallEventType.STATUS,
            event_value=CallStatus.INITIATED,
        )
    )
    return ok(_summary(call, form.patient_name))


async def _compile_and_stash(
    session: AsyncSession,
    call_plans: CallPlanStore,
    call: Call,
    form: PatientForm,
    tweak: PersonaTweak,
    room_name: str,
) -> list[str] | None:
    """Load the form's pinned schema version, build the DB prefill, compile the
    CallPlan, pin the call→prompt lineage, and stash the plan in Redis.

    Returns the sorted field_paths disclosed into the plan (for the caller's PHI
    audit) — the exact prefill set, so the audit is independent of how the
    compiler routes those values (confirm fields vs. prose placeholders).

    Returns None (stashing NOTHING) when the schema cannot compile — the worker
    then runs its static-persona fallback, mirroring its own fail-safe posture.
    A dead POST /calls on a platform-authoring bug is worse than a generic call,
    and legacy forms bound to a pre-DSL schema (empty schema_json) must keep
    working."""
    schema_version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one_or_none()  # global catalog (no RLS); RESTRICT FK → should exist
    if schema_version is None:
        raise ConflictError(message="the form's schema version is missing")

    # Published prompt version for the same schema family — the call→prompt→schema
    # lineage pin. Optional: a missing published prompt does not block the call.
    prompt_version_id = (
        await session.execute(
            select(PromptVersion.id)
            .join(Prompt, PromptVersion.prompt_id == Prompt.id)
            .where(
                Prompt.schema_id == schema_version.schema_id,
                PromptVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
    call.prompt_version_id = prompt_version_id

    # DB-known values: the intake payload leaves, overlaid by the current
    # field_answer rows (which include corrections and prior-call answers).
    values: dict[str, object] = dict(iter_leaf_answers(form.intake_payload))
    rows = (
        await session.execute(
            select(FieldAnswer.field_path, FieldAnswer.value).where(
                FieldAnswer.form_id == form.id, FieldAnswer.is_current
            )
        )
    ).all()
    for field_path, value_json in rows:
        values[field_path] = unwrap_value(value_json)
    prefill = build_prefill(values)

    try:
        plan = compile_call_plan(
            schema_version.schema_json,
            prefill,
            tweak,
            room_name=room_name,
            tenant_id=str(call.tenant_id),
            call_id=str(call.id),
            schema_version_id=str(schema_version.id),
            prompt_version_id=str(prompt_version_id) if prompt_version_id else None,
        )
    except CompileError:
        # A platform-authoring problem, not a caller problem. Log (message is
        # developer-authored, non-PHI) and start the call plan-less.
        logger.warning(
            "schema_version %s cannot compile into a call plan for %s — static fallback",
            schema_version.id,
            room_name,
            exc_info=True,
        )
        return None

    await call_plans.put_plan(plan)
    return sorted(prefill)


@router.get(
    "/calls/{call_id}/join-token",
    response_model=ResponseModel[JoinTokenResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def join_token(
    call_id: UUID,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[JoinTokenResponse]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    room_name = room_name_for_call(tenant_id, call.id)
    identity = f"supervisor-{caller.user_id}"
    token = livekit.mint_join_token(room_name=room_name, identity=identity)
    return ok(JoinTokenResponse(token=token, url=livekit.url, room_name=room_name))


@router.get(
    "/calls",
    response_model=ResponseModel[list[CallSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_calls(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary]]:
    rows = (
        await session.execute(
            select(Call, PatientForm.patient_name)
            .join(PatientForm, PatientForm.id == Call.form_id)
            .where(Call.current_status.in_(list(_ACTIVE_STATUSES)))
            .order_by(Call.created_at.desc())
        )
    ).all()
    return ok([_summary(c, name) for c, name in rows])


class UpdateCallStatusRequest(BaseModel):
    status: CallStatus


_TERMINAL_FAILURE_STATUSES = frozenset({CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY})

_ALLOWED_CALLBACK_STATUSES = frozenset(
    {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY}
)
_ALLOWED_CALLBACK_STATUS_VALUES = frozenset(s.value for s in _ALLOWED_CALLBACK_STATUSES)


@router.post(
    "/calls/{call_id}/status",
    response_model=ResponseModel[CallSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def update_call_status(
    request: Request,
    call_id: UUID,
    body: UpdateCallStatusRequest,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[CallSummary]:
    """Callback endpoint for the agent worker to report call terminal status.

    On terminal failure with retries remaining, auto-retries the form.
    Always fires the dispatcher afterward to fill freed concurrency slots.
    """
    call = (
        await session.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")

    # Validate the reported status before the idempotency short-circuit, so a bogus
    # or non-terminal status on an already-terminal call still gets a 422 (not a
    # silent 200 no-op).
    if body.status not in _ALLOWED_CALLBACK_STATUSES:
        allowed = ", ".join(s.value for s in _ALLOWED_CALLBACK_STATUSES)
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=f"only terminal statuses are accepted: {allowed}",
        )

    # Idempotent: if the call is already terminal, no-op.
    if call.current_status in _ALLOWED_CALLBACK_STATUS_VALUES:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == call.form_id))
        ).scalar_one_or_none()
        return ok(_summary(call, form.patient_name if form else None))

    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()

    # Update the call's status.
    call.current_status = body.status.value
    session.add(
        CallEvent(
            tenant_id=tenant_id,
            call_id=call.id,
            event_type=CallEventType.STATUS,
            event_value=body.status.value,
        )
    )

    sm = FormStateMachine()
    previous_form_status = form.status

    # An illegal form edge must not 500 the callback: the call's terminal status
    # is still recorded above even if the form can't take the transition (e.g. a
    # second call on the same form already moved it). Log and continue.
    try:
        if body.status == CallStatus.COMPLETED:
            sm.transition(form, FormStatus.COMPLETED, tenant_max_retries=tenant.max_retries)
        elif body.status in _TERMINAL_FAILURE_STATUSES:
            sm.transition(form, FormStatus.CALL_FAILED, tenant_max_retries=tenant.max_retries)
            # Auto-retry if retries remain; silently stay CALL_FAILED if exhausted.
            # The dispatcher creates the retry call on its next pass.
            with contextlib.suppress(InvalidTransitionError):
                sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant.max_retries)
                # Caller owns enqueued_at — use the DB clock to avoid cross-node skew.
                form.enqueued_at = func.now()
    except InvalidTransitionError:
        logger.warning(
            "call callback: form %s cannot transition from '%s' on call %s status '%s'; "
            "call status recorded, form left unchanged",
            form.id,
            previous_form_status,
            call_id,
            body.status.value,
        )

    await session.flush()

    audit = get_audit(request)
    # Audit the worker-driven form status change (HIPAA evidence trail).
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.SERVICE,
            actor_user_id=None,
            actor_label="agent-worker",
            event_type=AuditEvent.FORM_STATUS_CHANGE.value,
            resource_type="patient_form",
            resource_id=str(form.id),
            detail={
                "from": previous_form_status,
                "to": form.status,
                "call_id": str(call_id),
                "trigger": "call_callback",
            },
        )
    )

    # Fire the dispatcher — a concurrency slot just freed up.
    await try_dispatch(session, tenant_id, livekit, audit=audit)

    return ok(_summary(call, form.patient_name))
