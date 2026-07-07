"""Verification-call endpoints: create, join-token, active-list, publish,
revoke-access, and the agent-worker status callback.

Auth note (acknowledged stopgap): create / join-token / active-list guard
with `require("calls:read")` for now — the SPA has no real auth yet, and
the spec flags this. `publish` and `revoke-access` are owner-only actions
gated on `require("calls:publish")`: the caller must hold the permission
*and* be the call's `initiated_by_id`, enforced by an explicit 403 check
in each handler.
"""

import contextlib
import logging
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from control_plane.api.v1.common import Audit, LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import get_audit
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.ivr_selection import add_active_playbook_metadata
from control_plane.request_context import current_request_id
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord
from vera_core.models import Call, CallEvent, InsuranceProvider, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import CallEventType, CallStatus, FormStatus, ProviderStatus
from vera_core.observability.correlation import room_name_for_call
from vera_core.schemas import (
    CallSummary,
    JoinTokenResponse,
    PersonaTweak,
    RevokeAccessRequest,
    StartCallRequest,
)
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


def _supervisor_identity(user_id: UUID) -> str:
    """LiveKit participant identity for a VA joining/intervening on a call."""
    return f"supervisor-{user_id}"


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
        DefaultExceptionCode.VALIDATION_ERROR,
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
        initiated_by_id=caller.user_id,
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
    await livekit.create_call_room(room_name, metadata=metadata)
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
        # Ownerless calls are joinable tenant-wide; revoked users get the same
        # 404 as a private call (no enumeration).
        revoked = str(caller.user_id) in call.revoked_user_ids
        if revoked or (call.initiated_by_id is not None and not call.published):
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
                detail={"owner_id": str(call.initiated_by_id) if call.initiated_by_id else None},
            )
        )
    room_name = room_name_for_call(tenant_id, call.id)
    identity = _supervisor_identity(caller.user_id)
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
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[CallSummary]:
    response.headers["Cache-Control"] = "no-store"
    # RLS scopes to the caller's tenant; the row lock serializes concurrent publishes.
    call = (
        await session.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
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
    # Same row shape as list_calls — a None patient_name blanks the UI's
    # Patient cell until the next poll.
    patient_name = (
        await session.execute(
            select(PatientForm.patient_name).where(PatientForm.id == call.form_id)
        )
    ).scalar_one_or_none()
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="call",
            resource_id=str(call.id),
            request_id=current_request_id(request),
            detail={"fields": ["patient_name"]},
        )
    )
    return ok(_summary(call, patient_name, caller.user_id))


@router.get(
    "/calls",
    response_model=ResponseModel[list[CallSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_calls(
    request: Request,
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary]]:
    response.headers["Cache-Control"] = "no-store"
    rows = (
        await session.execute(
            select(Call, PatientForm.patient_name)
            .join(PatientForm, PatientForm.id == Call.form_id)
            .where(Call.current_status.in_(list(_ACTIVE_STATUSES)))
            # Ownerless (pre-ownership dispatcher) calls are tenant-visible —
            # hidden, they'd have no monitoring and no owner to ever publish them.
            .where(
                or_(
                    Call.initiated_by_id == caller.user_id,
                    Call.published.is_(True),
                    Call.initiated_by_id.is_(None),
                )
            )
            .order_by(Call.created_at.desc())
        )
    ).all()
    # PHI disclosure (patient_name) — audit field names, mirroring list_patient_forms.
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="call",
            resource_id="list",
            request_id=current_request_id(request),
            detail={"fields": ["patient_name"]},
        )
    )
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
    # Row lock: concurrent revokes must not overwrite each other's list append.
    call = (
        await session.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="only the owner can revoke access"
        )
    target = str(body.target_user_id)
    if target not in call.revoked_user_ids:
        call.revoked_user_ids = [*call.revoked_user_ids, target]
    room_name = room_name_for_call(tenant_id, call.id)
    await livekit.remove_participant(room_name, _supervisor_identity(body.target_user_id))
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
    caller: VerifiedIdentity = require("calls:read"),
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
        return ok(_summary(call, form.patient_name if form else None, caller.user_id))

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

    return ok(_summary(call, form.patient_name, caller.user_id))
