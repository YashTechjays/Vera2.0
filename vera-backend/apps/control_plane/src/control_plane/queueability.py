"""Enqueue-time gates for `PUT /patient-forms/{id}/status` → IN_QUEUE, run BEFORE the
state-machine transition: `ensure_queueable` rejects a form that could never be dialed
(no payer phone, no outbound trunk); `ensure_va_capacity` rejects an enqueue that would
put the caller past the tenant's per-VA in-flight limit. Working hours stay dial-time
concerns the dispatcher handles.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode

# Single source of truth in vera_core.forms.intake. Used locally below and re-exported
# (explicit `as`) for api/v1/voice_lab.py, which imports E164_RE from this module.
from vera_core.forms.intake import E164_RE as E164_RE
from vera_core.integrations.credentials import get_integration_credentials
from vera_core.models import PatientForm
from vera_core.models.enums import FormStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from vera_core.config.kms import KeyManagementService
    from vera_core.models import Tenant

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


# Distinct namespace from the dispatcher's _DISPATCH_LOCK_CLASS (0x76455241 "vERA").
_ENQUEUE_LOCK_CLASS = 0x76455251  # "vERQ"

# The per-VA in-flight set: a queued form is a claimed agent slot, not just a live call.
IN_FLIGHT_FORM_STATUSES: tuple[str, ...] = (
    FormStatus.IN_QUEUE.value,
    FormStatus.IN_CALL.value,
    FormStatus.AI_PROCESSING.value,
)


async def ensure_va_capacity(
    session: "AsyncSession", tenant: "Tenant", caller_user_id: UUID
) -> None:
    """Raise CONFLICT if the caller is already at the tenant's per-VA in-flight limit."""
    # Transaction-scoped advisory lock serializes same-VA concurrent enqueues (the
    # double-click race) so two counts can't both pass at limit-1; releases on
    # commit/rollback. Different VAs hash to different locks — no cross-VA contention.
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                _ENQUEUE_LOCK_CLASS, func.hashtext(f"{tenant.id}:{caller_user_id}")
            )
        )
    )
    in_flight: int = (
        await session.execute(
            select(func.count())
            .select_from(PatientForm)
            .where(
                PatientForm.tenant_id == tenant.id,
                PatientForm.enqueued_by_id == caller_user_id,
                PatientForm.status.in_(IN_FLIGHT_FORM_STATUSES),
            )
        )
    ).scalar_one()
    if in_flight >= tenant.max_agents_per_va:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message=(
                f"You are at your concurrent-agent limit ({tenant.max_agents_per_va}). "
                "Wait for a call to finish or ask your admin to raise the limit."
            ),
            data={"limit": tenant.max_agents_per_va, "in_flight": in_flight},
        )
