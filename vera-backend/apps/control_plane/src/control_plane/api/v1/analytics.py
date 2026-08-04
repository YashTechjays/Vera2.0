"""Tenant analytics: live queue status, the live provider panel, the history report.

Counts, averages, and catalog names only — no patient field ever leaves this module,
which is what exempts it from the PHI display-path audit (precedent: calls.py::call_stats).
"""

from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import and_, func, select

from control_plane.api.v1.calls import ACTIVE_CALL_STATUSES
from control_plane.api.v1.common import TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.call_authz import visible_to
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.models import Call, InsuranceProvider, PatientForm, Tenant
from vera_core.models.enums import FormStatus, ProviderStatus
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


class LiveProviderRow(BaseModel):
    provider_id: UUID | None
    provider_name: str | None  # None => the frontend's "(No provider)" bucket
    in_queue: int
    active: int


class LivePanel(BaseModel):
    rows: list[LiveProviderRow]


@router.get(
    "/analytics/live",
    response_model=ResponseModel[LivePanel],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def live_panel(
    response: Response,
    tenant_id: TenantId,
    session: TenantSession,
    caller: VerifiedIdentity = require("reports:dashboard"),
) -> ResponseModel[LivePanel]:
    """Live counts per provider: `active` mirrors Live Monitoring's status set and
    per-user visibility, `in_queue` resolves each form's free-text provider the way
    the dispatcher does."""
    response.headers["Cache-Control"] = "no-store"
    active_rows = (
        await session.execute(
            select(Call.insurance_provider_id, func.count())
            .where(
                Call.current_status.in_(list(ACTIVE_CALL_STATUSES)),
                visible_to(caller.user_id),
            )
            .group_by(Call.insurance_provider_id)
        )
    ).all()
    queued_rows = (
        await session.execute(
            select(InsuranceProvider.id, func.count())
            .select_from(PatientForm)
            .outerjoin(
                InsuranceProvider,
                and_(
                    # Same match as queue_dispatcher._resolve_provider.
                    func.lower(InsuranceProvider.name)
                    == func.lower(func.trim(PatientForm.insurance_provider)),
                    InsuranceProvider.status == ProviderStatus.ACTIVE.value,
                ),
            )
            .where(PatientForm.status == FormStatus.IN_QUEUE.value)
            .group_by(InsuranceProvider.id)
        )
    ).all()
    counts: dict[UUID | None, dict[str, int]] = defaultdict(lambda: {"in_queue": 0, "active": 0})
    for provider_id, n in active_rows:
        counts[provider_id]["active"] = n
    for provider_id, n in queued_rows:
        counts[provider_id]["in_queue"] = n
    named = [pid for pid in counts if pid is not None]
    names: dict[UUID, str] = {}
    if named:
        name_rows = (
            await session.execute(
                select(InsuranceProvider.id, InsuranceProvider.name).where(
                    InsuranceProvider.id.in_(named)
                )
            )
        ).all()
        for pid, name in name_rows:
            names[pid] = name
    rows = sorted(
        (
            LiveProviderRow(
                provider_id=pid,
                provider_name=names.get(pid) if pid is not None else None,
                in_queue=c["in_queue"],
                active=c["active"],
            )
            for pid, c in counts.items()
        ),
        # Named providers alphabetically; the no-provider bucket last.
        key=lambda r: (r.provider_name is None, r.provider_name or ""),
    )
    return ok(LivePanel(rows=rows))
