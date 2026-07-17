"""Shared DTOs crossing the control-plane API boundary."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CallSummary(BaseModel):
    """Verification-call list/summary row for Live Monitoring."""

    id: UUID
    tenant_id: UUID
    # The patient form this call verifies — lets the UI open the real form.
    form_id: UUID
    status: str
    room_name: str
    patient_name: str | None = None
    insurance_provider: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    published: bool = False
    is_owner: bool = False


class CallStats(BaseModel):
    """Live Monitoring stat cards — counts over the calls visible to the caller."""

    total_today: int
    live: int
    critical: int


class JoinTokenResponse(BaseModel):
    token: str
    url: str
    room_name: str


class StartVoiceSessionRequest(BaseModel):
    """Voice Lab session request — talk to the bot in-browser or dial out via SIP."""

    mode: Literal["browser", "outbound"]
    phone_number: str | None = None  # required + E.164 when mode == "outbound"
    enable_ivr_navigation: bool = False  # ON → worker boots the IVR navigator agent
    # Optional provider to test a specific playbook: with enable_ivr_navigation ON, its active
    # playbook (if any) specializes the navigator; else the navigator runs generic.
    insurance_provider_id: UUID | None = None


class VoiceSessionResponse(BaseModel):
    """LiveKit join details for the browser to connect to the Voice Lab room."""

    room_name: str
    url: str  # settings.livekit_url, for the browser SDK
    token: str  # browser join JWT
    mode: str


class RetentionPolicy(BaseModel):
    """Tenant recording-retention knob. retention_days=None → the platform
    default applies (surfaced as default_days so the UI can render the
    effective value); otherwise bounded to 1 day..10 years."""

    retention_days: int | None = Field(default=None, ge=1, le=3650)
    default_days: int


class RetentionPolicyUpdate(BaseModel):
    """PATCH body: None retention_days reverts the tenant to the platform default."""

    retention_days: int | None = Field(default=None, ge=1, le=3650)


class RecordingPlayback(BaseModel):
    """A short-lived signed URL for one recording. The URL itself is the
    credential — never logged, never cached (Cache-Control: no-store)."""

    url: str
    expires_at: datetime
