"""Tenant analytics: live queue status, the live provider panel, the history report.

Counts, averages, and catalog names only — no patient field ever leaves this module,
which is what exempts it from the PHI display-path audit (precedent: calls.py::call_stats).
"""

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from control_plane.api.v1.common import TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.models import PatientForm, Tenant
from vera_core.models.enums import FormStatus
from vera_core.services.queue_dispatcher import DISPATCH_ACTIVE_FORM_STATUSES

router = APIRouter(tags=["analytics"])


class QueueStatus(BaseModel):
    """Tenant-wide mirror of the dispatcher's slot math (queue_dispatcher.try_dispatch)."""

    limit: int
    active: int
    in_queue: int


@router.get(
    "/analytics/queue-status",
    response_model=ResponseModel[QueueStatus],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def queue_status(
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[QueueStatus]:
    """Why a queued call hasn't dialed yet — tenant-wide (not per-user) on purpose,
    since the dial ceiling is shared."""
    response.headers["Cache-Control"] = "no-store"
    limit = (
        await session.execute(select(Tenant.max_concurrent_calls).where(Tenant.id == tenant_id))
    ).scalar_one()
    active, in_queue = (
        await session.execute(
            select(
                func.count().filter(PatientForm.status.in_(list(DISPATCH_ACTIVE_FORM_STATUSES))),
                func.count().filter(PatientForm.status == FormStatus.IN_QUEUE.value),
            ).select_from(PatientForm)
        )
    ).one()
    return ok(QueueStatus(limit=limit, active=active, in_queue=in_queue))
