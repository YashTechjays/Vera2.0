"""Verification-call endpoints: create, join-token, active-list, publish,
and revoke-access.

Auth note (acknowledged stopgap): create / join-token / active-list guard
with `require("calls:read")` for now — the SPA has no real auth yet, and
the spec flags this. `publish` and `revoke-access` are owner-only actions
gated on `require("calls:publish")`: the caller must hold the permission
*and* be the call's `initiated_by_id`, enforced by an explicit 403 check
in each handler.
"""

from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import func, or_, select

from control_plane.api.v1.common import Audit, LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.request_context import current_request_id
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord
from vera_core.models import Call, CallEvent, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallEventType, CallStatus, FormStatus
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import (
    CallSummary,
    JoinTokenResponse,
    PersonaTweak,
    RevokeAccessRequest,
    StartCallRequest,
)

router = APIRouter(tags=["calls"])

_ACTIVE_STATUSES = (
    CallStatus.INITIATED,
    CallStatus.RINGING,
    CallStatus.IVR,
    CallStatus.ACTIVE,
    CallStatus.WAITING,
    CallStatus.CRITICAL,
)


def _summary(call: Call, patient_name: str | None, caller_id: UUID) -> CallSummary:
    return CallSummary(
        id=call.id,
        tenant_id=call.tenant_id,
        status=call.current_status,
        room_name=room_name_for_call(call.tenant_id, call.id),
        patient_name=patient_name,
        started_at=call.started_at,
        created_at=call.created_at,
        published=call.published,
        is_owner=call.initiated_by_id == caller_id,
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
    caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
) -> ResponseModel[CallSummary]:
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == body.form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    call = Call(
        tenant_id=tenant_id,
        form_id=form.id,
        current_status=CallStatus.INITIATED,
        initiated_by_id=caller.user_id,
    )
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
    return ok(_summary(call, form.patient_name, caller.user_id))


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
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[JoinTokenResponse]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:  # non-owner joining another's call
        if not call.published:
            raise NotFoundError(message="call not found")  # don't reveal a private call
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_user_id=caller.user_id,
                actor_label=caller.email or caller.subject,
                event_type=AuditEvent.CALL_INTERVENE_JOIN.value,
                resource_type="call",
                resource_id=str(call.id),
                permission_key="calls:read",
                decision="allow",
                request_id=current_request_id(request),
                detail={"owner_id": str(call.initiated_by_id)},
            )
        )
    room_name = room_name_for_call(tenant_id, call.id)
    identity = f"supervisor-{caller.user_id}"
    token = livekit.mint_join_token(room_name=room_name, identity=identity)
    return ok(JoinTokenResponse(token=token, url=livekit.url, room_name=room_name))


@router.post(
    "/calls/{call_id}/publish",
    response_model=ResponseModel[CallSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def publish_call(
    call_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[CallSummary]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS scopes to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="only the owner can publish"
        )
    if not call.published:  # idempotent, one-way — no un-publish
        call.published = True
        call.published_at = func.now()
        await audit.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                actor_user_id=caller.user_id,
                actor_label=caller.email or caller.subject,
                event_type=AuditEvent.CALL_PUBLISH.value,
                resource_type="call",
                resource_id=str(call.id),
                permission_key="calls:publish",
                decision="allow",
                request_id=current_request_id(request),
            )
        )
    return ok(_summary(call, None, caller.user_id))


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
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary]]:
    rows = (
        await session.execute(
            select(Call, PatientForm.patient_name)
            .join(PatientForm, PatientForm.id == Call.form_id)
            .where(Call.current_status.in_(list(_ACTIVE_STATUSES)))
            .where(or_(Call.initiated_by_id == caller.user_id, Call.published.is_(True)))
            .order_by(Call.created_at.desc())
        )
    ).all()
    return ok([_summary(c, name, caller.user_id) for c, name in rows])


@router.post(
    "/calls/{call_id}/revoke-access",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def revoke_access(
    call_id: UUID,
    body: RevokeAccessRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[None]:
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS scopes to the caller's tenant
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="only the owner can revoke access"
        )
    room_name = room_name_for_call(tenant_id, call.id)
    await livekit.remove_participant(room_name, f"supervisor-{body.target_user_id}")
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.CALL_INTERVENE_REVOKE.value,
            resource_type="call",
            resource_id=str(call.id),
            permission_key="calls:publish",
            decision="allow",
            request_id=current_request_id(request),
            detail={"target_user_id": str(body.target_user_id)},
        )
    )
    return ok(None, message="Access revoked.")
