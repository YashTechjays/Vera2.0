"""Verification-call endpoints: create, join-token, active-list.

Auth note (acknowledged stopgap): all three endpoints guard with
`require("calls:read")` for now — the SPA has no real auth yet, and the
spec flags this.  A later task tightens POST / join-token to a write /
manage permission once the RBAC catalog is extended.
"""

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from control_plane.api.v1.common import LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode, NotFoundError
from control_plane.ivr_selection import add_active_playbook_metadata
from control_plane.responses import ResponseModel, ok
from vera_core.models import Call, CallEvent, InsuranceProvider, PatientForm, Tenant
from vera_core.models.enums import CallEventType, CallStatus, FormStatus
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import CallSummary, JoinTokenResponse, PersonaTweak, StartCallRequest

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
    if body.insurance_provider_id is not None:
        # Checked here so an unknown id is a 404, not an FK violation → 500 at flush.
        provider_exists = (
            await session.execute(
                select(InsuranceProvider.id).where(
                    InsuranceProvider.id == body.insurance_provider_id
                )
            )
        ).scalar_one_or_none()
        if provider_exists is None:
            raise NotFoundError(message="unknown insurance provider")

    call = Call(
        tenant_id=tenant_id,
        form_id=form.id,
        current_status=CallStatus.INITIATED,
        insurance_provider_id=body.insurance_provider_id,
    )
    session.add(call)
    await session.flush()  # populates call.id (UUIDv7)

    room_name = room_name_for_call(tenant_id, call.id)
    persona = (
        await session.execute(select(Tenant.persona_tweak).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()  # RLS on `tenant` keys on id → only the caller's own row
    # persona_tweak is admin-authored, non-PHI config; safe to serialize into metadata.
    # Nested under its own key so sibling dispatch keys never trip the worker's
    # extra="forbid" PersonaTweak validation (see agent_worker.prompt.parse_persona_tweak).
    tweak = PersonaTweak.model_validate(persona) if persona is not None else PersonaTweak()
    metadata: dict[str, object] = {}
    if tweak_fields := tweak.model_dump(exclude_none=True):
        metadata["persona_tweak"] = tweak_fields
    # When navigating the payer IVR, specialize the navigator with the provider's active playbook
    # (non-PHI overlay) if one exists; otherwise it runs generic. Off preserves today's behavior.
    if body.enable_ivr_navigation:
        metadata["enable_ivr_navigation"] = True
        await add_active_playbook_metadata(session, body.insurance_provider_id, metadata)
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
