"""Shared DTOs crossing the control-plane API boundary."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CallSummary(BaseModel):
    """Placeholder shape for the verification-call list.

    TODO(vera-2.x): grows alongside the real calls table (status, payer,
    outbound SIP dispatch state, recording pointer, extraction results).
    """

    id: UUID
    tenant_id: UUID
    status: str
    created_at: datetime
