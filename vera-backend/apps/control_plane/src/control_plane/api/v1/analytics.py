"""Tenant analytics: live queue status, the live provider panel, the history report.

Counts, averages, and catalog names only — no patient field ever leaves this module,
which is what exempts it from the PHI display-path audit (precedent: calls.py::call_stats).
"""

from collections import Counter
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.calls import ACTIVE_CALL_STATUSES
from control_plane.api.v1.common import TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.call_authz import visible_to
from control_plane.call_closeout import TERMINAL_VALUES
from control_plane.exceptions import CustomAPIException, CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.models import (
    AppUser,
    Call,
    InsuranceProvider,
    InterventionEvent,
    PatientForm,
    Tenant,
)
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
    _tenant_id: TenantId,
    session: TenantSession,
    caller: VerifiedIdentity = require("reports:dashboard"),
) -> ResponseModel[LivePanel]:
    """Live counts per provider: `active` mirrors Live Monitoring's status set and
    per-user visibility, `in_queue` resolves each form's free-text provider the way
    the dispatcher does."""
    response.headers["Cache-Control"] = "no-store"
    active_result = await session.execute(
        select(Call.insurance_provider_id, func.count())
        .where(
            Call.current_status.in_(list(ACTIVE_CALL_STATUSES)),
            visible_to(caller.user_id),
        )
        .group_by(Call.insurance_provider_id)
    )
    queued_result = await session.execute(
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
    # A None key is the no-provider bucket: no provider on the call, or queued free
    # text the outer join couldn't match.
    active_counts: dict[UUID | None, int] = dict(active_result.tuples().all())
    queued_counts: dict[UUID | None, int] = dict(queued_result.tuples().all())
    provider_ids = active_counts.keys() | queued_counts.keys()
    catalog_ids = [pid for pid in provider_ids if pid is not None]
    names: dict[UUID, str] = {}
    if catalog_ids:
        name_rows = await session.execute(
            select(InsuranceProvider.id, InsuranceProvider.name).where(
                InsuranceProvider.id.in_(catalog_ids)
            )
        )
        names = dict(name_rows.tuples().all())
    rows = sorted(
        (
            LiveProviderRow(
                provider_id=pid,
                provider_name=names.get(pid) if pid is not None else None,
                in_queue=queued_counts.get(pid, 0),
                active=active_counts.get(pid, 0),
            )
            for pid in provider_ids
        ),
        # Named providers alphabetically; the no-provider bucket last.
        key=lambda r: (r.provider_name is None, r.provider_name or ""),
    )
    return ok(LivePanel(rows=rows))


_MAX_RANGE = timedelta(days=366)


class ReportMetrics(BaseModel):
    call_volume: int
    avg_duration_seconds: float | None
    avg_completion_pct: float | None
    intervened_calls: int
    intervention_rate: float | None  # 0..1


class DayCount(BaseModel):
    day: date
    calls: int


class InterventionTypeCount(BaseModel):
    type: str
    count: int


class InterventionDayCounts(BaseModel):
    """One field per InterventionType value — extend together with the enum."""

    day: date
    flag: int = 0
    coach: int = 0
    whisper: int = 0
    takeover: int = 0


class HistoryReport(BaseModel):
    current: ReportMetrics
    previous: ReportMetrics
    calls_per_day: list[DayCount]
    interventions_by_type: list[InterventionTypeCount]
    interventions_per_day: list[InterventionDayCounts]


class FilterOption(BaseModel):
    id: UUID
    name: str


class ReportFilterOptions(BaseModel):
    providers: list[FilterOption]
    vas: list[FilterOption]


def _call_window(
    date_from: datetime,
    date_to: datetime,
    provider_id: UUID | None,
    va_id: UUID | None,
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = [Call.created_at >= date_from, Call.created_at < date_to]
    if provider_id is not None:
        conds.append(Call.insurance_provider_id == provider_id)
    if va_id is not None:
        conds.append(Call.initiated_by_id == va_id)
    return conds


async def _window_metrics(session: AsyncSession, conds: list[ColumnElement[bool]]) -> ReportMetrics:
    volume, avg_duration, avg_completion = (
        await session.execute(
            select(
                func.count(),
                func.avg(func.extract("epoch", Call.ended_at - Call.started_at)).filter(
                    Call.started_at.is_not(None), Call.ended_at.is_not(None)
                ),
                # Completion is frozen onto the call at terminal status (call_lifecycle);
                # average only terminal calls that connected — a dead dial would smuggle
                # the form's frozen completion into the metric, and live calls read 0.
                func.avg(Call.completion_pct).filter(
                    Call.current_status.in_(TERMINAL_VALUES), Call.started_at.is_not(None)
                ),
            )
            .select_from(Call)
            .where(*conds)
        )
    ).one()
    intervened = (
        await session.execute(
            select(func.count(func.distinct(InterventionEvent.call_id)))
            .select_from(InterventionEvent)
            .join(Call, Call.id == InterventionEvent.call_id)
            .where(*conds)
        )
    ).scalar_one()
    return ReportMetrics(
        call_volume=volume,
        avg_duration_seconds=float(avg_duration) if avg_duration is not None else None,
        avg_completion_pct=float(avg_completion) if avg_completion is not None else None,
        intervened_calls=intervened,
        intervention_rate=(intervened / volume) if volume else None,
    )


@router.get(
    "/analytics/report",
    response_model=ResponseModel[HistoryReport],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def history_report(
    response: Response,
    _tenant_id: TenantId,
    session: TenantSession,
    date_from: datetime,
    date_to: datetime,
    provider_id: UUID | None = None,
    va_id: UUID | None = None,
    _caller: VerifiedIdentity = require("reports:dashboard"),
) -> ResponseModel[HistoryReport]:
    """Tenant-wide historical metrics, computed from the raw rows at request time so a
    manual spot-check over the same range always matches."""
    response.headers["Cache-Control"] = "no-store"
    if date_to <= date_from:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR, message="date_to must be after date_from"
        )
    if date_to - date_from > _MAX_RANGE:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR, message="date range is capped at 366 days"
        )
    current_conds = _call_window(date_from, date_to, provider_id, va_id)
    prev_from = date_from - (date_to - date_from)
    previous_conds = _call_window(prev_from, date_from, provider_id, va_id)

    current = await _window_metrics(session, current_conds)
    previous = await _window_metrics(session, previous_conds)

    # UTC day buckets — same convention as calls.py::call_stats "today".
    day = func.timezone("UTC", func.date_trunc("day", func.timezone("UTC", Call.created_at)))
    day_rows = (
        await session.execute(
            select(day.label("day"), func.count())
            .select_from(Call)
            .where(*current_conds)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    # Interventions bucket by their CALL's day so every report series shares one axis.
    type_day_rows = (
        await session.execute(
            select(day.label("day"), InterventionEvent.type, func.count())
            .select_from(InterventionEvent)
            .join(Call, Call.id == InterventionEvent.call_id)
            .where(*current_conds)
            .group_by(day, InterventionEvent.type)
        )
    ).all()
    per_day: dict[date, dict[str, int]] = {}
    type_totals: Counter[str] = Counter()
    for d, t, n in type_day_rows:
        per_day.setdefault(d.date(), {})[t] = n
        type_totals[t] += n
    return ok(
        HistoryReport(
            current=current,
            previous=previous,
            calls_per_day=[DayCount(day=d.date(), calls=n) for d, n in day_rows],
            interventions_by_type=[
                InterventionTypeCount(type=t, count=n) for t, n in sorted(type_totals.items())
            ],
            interventions_per_day=[
                InterventionDayCounts(day=d, **counts) for d, counts in sorted(per_day.items())
            ],
        )
    )


@router.get(
    "/analytics/filters",
    response_model=ResponseModel[ReportFilterOptions],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def report_filters(
    response: Response,
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("reports:dashboard"),
) -> ResponseModel[ReportFilterOptions]:
    """Filter options: the active provider catalog (global, non-PHI) and the tenant
    users who have initiated calls (workforce identity, not patient data)."""
    response.headers["Cache-Control"] = "no-store"
    providers = (
        await session.execute(
            select(InsuranceProvider.id, InsuranceProvider.name)
            .where(InsuranceProvider.status == ProviderStatus.ACTIVE.value)
            .order_by(InsuranceProvider.name)
        )
    ).all()
    vas = (
        await session.execute(
            select(AppUser.id, AppUser.name, AppUser.email)
            .join(Call, Call.initiated_by_id == AppUser.id)
            .distinct()
            .order_by(AppUser.name, AppUser.email)
        )
    ).all()
    return ok(
        ReportFilterOptions(
            providers=[FilterOption(id=i, name=n) for i, n in providers],
            vas=[FilterOption(id=i, name=name or email) for i, name, email in vas],
        )
    )
