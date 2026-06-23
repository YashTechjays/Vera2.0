"""Voice Lab — a standalone developer/QA harness to start a voice session with
the Vera agent and listen to it live in the browser.

No persistence: this creates an ephemeral LiveKit room, dispatches the agent, and
(for outbound) places a SIP call — but writes no Call/PatientForm rows and never
appears in Live Monitoring. It is deliberately decoupled from /calls so it can be
built and tested in parallel with the real call-initiation flow.

Auth note (acknowledged stopgap): guards with `require("calls:read")`, matching the
interim convention in `calls.py`.
"""

import re

from fastapi import APIRouter

from control_plane.api.v1.common import AppSettings, LiveKit, TenantId
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.exceptions import (
    ConflictError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.db import uuid7
from vera_core.observability.correlation import (
    CALLER_IDENTITY_PREFIX,
    MONITOR_IDENTITY_PREFIX,
    parse_room_name,
    room_name_for_call,
)
from vera_core.schemas import StartVoiceSessionRequest, VoiceSessionResponse

router = APIRouter(tags=["voice-lab"])

# E.164: a leading + and 1-15 digits, first digit non-zero.
_E164 = re.compile(r"^\+[1-9]\d{1,14}$")


@router.post(
    "/voice-lab/sessions",
    response_model=ResponseModel[VoiceSessionResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def start_voice_session(
    body: StartVoiceSessionRequest,
    tenant_id: TenantId,
    livekit: LiveKit,
    settings: AppSettings,
    caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
) -> ResponseModel[VoiceSessionResponse]:
    # Synthetic call id — no DB row; the room name is still the canonical
    # call--<tenant>--<call> so worker correlation/observability work unchanged.
    room_name = room_name_for_call(tenant_id, uuid7())

    # The browser always joins to hear the conversation. In browser mode it also
    # publishes the mic (caller-); in outbound mode it is listen-only (monitor-),
    # which the worker's wait_for_speaker skips when deciding the room is ready.
    is_outbound = body.mode == "outbound"
    if is_outbound:
        if settings.livekit_sip_trunk_id is None:
            raise ConflictError(message="outbound SIP is not configured")
        if body.phone_number is None or not _E164.match(body.phone_number):
            raise CustomAPIException(
                DefaultExceptionCode.VALIDATION_ERROR,
                message="phone_number must be E.164 for an outbound call",
            )

    prefix = MONITOR_IDENTITY_PREFIX if is_outbound else CALLER_IDENTITY_PREFIX
    browser_identity = f"{prefix}{caller.user_id}"

    await livekit.create_call_room(room_name, metadata={"wait_for_speaker": True})
    if is_outbound:
        assert body.phone_number is not None  # validated non-None above when is_outbound
        await livekit.create_sip_participant(room_name, body.phone_number)

    token = livekit.mint_join_token(room_name=room_name, identity=browser_identity)
    return ok(
        VoiceSessionResponse(
            room_name=room_name,
            url=livekit.url,
            token=token,
            mode=body.mode,
        )
    )


@router.delete(
    "/voice-lab/sessions/{room_name}",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def end_voice_session(
    room_name: str,
    tenant_id: TenantId,
    livekit: LiveKit,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[None]:
    # Deleting the room is what actually ends the session: it disconnects the agent
    # worker (its session shuts down) and any SIP callee (the outbound call hangs up).
    # The browser leaving on its own does none of that. Tenant-scope by the room name
    # (which embeds the tenant uuid) so one tenant can't tear down another's room.
    ref = parse_room_name(room_name)
    if ref is None or ref.tenant_id != tenant_id:
        raise NotFoundError(message="voice session not found")
    await livekit.delete_room(room_name)
    return ok(None, message="Voice session ended.")
