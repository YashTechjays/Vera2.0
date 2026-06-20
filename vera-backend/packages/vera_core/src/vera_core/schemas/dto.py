"""Shared DTOs crossing the control-plane API boundary."""

from datetime import datetime
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
