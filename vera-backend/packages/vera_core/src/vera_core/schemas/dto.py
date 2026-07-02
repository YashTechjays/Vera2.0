"""Shared DTOs crossing the control-plane API boundary."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class CallSummary(BaseModel):
    """Verification-call list/summary row for Live Monitoring."""

    id: UUID
    tenant_id: UUID
    status: str
    room_name: str
    patient_name: str | None = None
    started_at: datetime | None = None
    created_at: datetime


class StartCallRequest(BaseModel):
    form_id: UUID


class JoinTokenResponse(BaseModel):
    token: str
    url: str
    room_name: str


class StartVoiceSessionRequest(BaseModel):
    """Voice Lab session request — talk to the bot in-browser or dial out via SIP."""

    mode: Literal["browser", "outbound"]
    phone_number: str | None = None  # required + E.164 when mode == "outbound"
    enable_ivr_navigation: bool = False  # ON → worker boots the generic IVR navigator agent


class VoiceSessionResponse(BaseModel):
    """LiveKit join details for the browser to connect to the Voice Lab room."""

    room_name: str
    url: str  # settings.livekit_url, for the browser SDK
    token: str  # browser join JWT
    mode: str
