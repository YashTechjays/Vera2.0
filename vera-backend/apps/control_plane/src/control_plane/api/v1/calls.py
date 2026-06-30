"""Verification-call endpoints: create, join-token, active-list.

Auth note (acknowledged stopgap): all three endpoints guard with
`require("calls:read")` for now — the SPA has no real auth yet, and the
spec flags this.  A later task tightens POST / join-token to a write /
manage permission once the RBAC catalog is extended.
"""

import contextlib
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from control_plane.api.v1.common import LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import get_audit
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord
from vera_core.models import Call, CallEvent, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallEventType, CallStatus, FormStatus
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import CallSummary, JoinTokenResponse, PersonaTweak, StartCallRequest
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.queue_dispatcher import try_dispatch

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
    ),
)
async def start_call(
    body: StartCallRequest,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    _caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
) -> ResponseModel[CallSummary]:
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == body.form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    call = Call(tenant_id=tenant_id, form_id=form.id, current_status=CallStatus.INITIATED)
    session.add(call)
    await session.flush()  # populates call.id (UUIDv7)

    room_name = room_name_for_call(tenant_id, call.id)
    persona = (
        await session.execute(select(Tenant.persona_tweak).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()  # RLS on `tenant` keys on id → only the caller's own row
    # persona_tweak is admin-authored, non-PHI config; safe to serialize into metadata.
    tweak = PersonaTweak.model_validate(persona) if persona is not None else PersonaTweak()
    metadata = tweak.model_dump(exclude_none=True)
    await livekit.create_call_room(room_name, metadata=metadata)
    form.status = FormStatus.IN_QUEUE
    session.add(
        CallEvent(
            tenant_id=tenant_id,
            call_id=call.id,
            event_type=CallEventType.STATUS,
            event_value=CallStatus.INITIATED,
        )
    )
    return ok(_summary(call, form.patient_name))


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

    # Idempotent: if the call is already terminal, no-op.
    if call.current_status in {s.value for s in _ALLOWED_CALLBACK_STATUSES}:
        form = (
            await session.execute(select(PatientForm).where(PatientForm.id == call.form_id))
        ).scalar_one_or_none()
        return ok(_summary(call, form.patient_name if form else None))

    if body.status not in _ALLOWED_CALLBACK_STATUSES:
        allowed = ", ".join(s.value for s in _ALLOWED_CALLBACK_STATUSES)
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=f"only terminal statuses are accepted: {allowed}",
        )

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

    if body.status == CallStatus.COMPLETED:
        sm.transition(form, FormStatus.COMPLETED, tenant_max_retries=tenant.max_retries)
    elif body.status in _TERMINAL_FAILURE_STATUSES:
        sm.transition(form, FormStatus.CALL_FAILED, tenant_max_retries=tenant.max_retries)
        # Auto-retry if retries remain; silently stay CALL_FAILED if exhausted.
        # Record retry lineage — the next call (created by dispatcher) will
        # link back to this one. For now we just mark the form as re-queued;
        # the dispatcher creates the Call + CallLineage on its next pass.
        with contextlib.suppress(InvalidTransitionError):
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant.max_retries)
            # Caller owns enqueued_at — use the DB clock to avoid cross-node skew.
            form.enqueued_at = func.now()

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
