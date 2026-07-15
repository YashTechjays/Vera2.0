"""Enqueue-time dialability gate. `PUT /patient-forms/{id}/status` → IN_QUEUE calls
this BEFORE the state-machine transition so a form that could never be dialed is
rejected with an actionable error instead of sitting in queue until expiry.

Deliberately narrow: only hard blockers (no payer phone, no outbound trunk). Soft
conditions the dispatcher already handles at dial time (working hours, concurrency)
are NOT re-checked here.
"""

from typing import TYPE_CHECKING

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from vera_core.forms.intake import E164_RE as E164_RE
from vera_core.integrations.credentials import get_integration_credentials

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from vera_core.config.kms import KeyManagementService
    from vera_core.models import PatientForm

TRUNK_INTEGRATION = "livekit_outbound_trunk_id"


async def ensure_queueable(
    session: "AsyncSession", kms: "KeyManagementService", form: "PatientForm"
) -> None:
    """Raise if *form* cannot possibly be dialed once dispatched."""
    phone = form.insurance_provider_phone_number
    if not phone or not E164_RE.match(phone):
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="form has no valid insurance provider phone number (E.164 required)",
            data={"field": "insurance_provider_phone_number"},
        )
    creds = await get_integration_credentials(session, kms, integration_type_name=TRUNK_INTEGRATION)
    if not (creds or {}).get("trunk_id"):
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message="outbound calling is not configured for this tenant (missing SIP trunk)",
        )
