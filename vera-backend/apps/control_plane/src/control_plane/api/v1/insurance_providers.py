"""Platform (SUPER_ADMIN) insurance-provider catalog routes.

The insurance_provider catalog is GLOBAL (no tenant_id, no RLS, no PHI) and curated by a
platform operator — reference data shared across tenants that IVR playbooks attach to.
Authorization is platform_require (account_type='platform' + platform:ivr_playbooks grants);
no tenant context. Mirrors api/v1/prompts.py.
"""

from datetime import datetime, time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import AppSettings
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import get_idempotency_store, platform_scoped_session
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode
from control_plane.idempotency import (
    PLATFORM_IDEM_SCOPE,
    claim_or_conflict,
    require_idempotency_key,
)
from control_plane.responses import ResponseModel, ok
from vera_core.models import InsuranceProvider

router = APIRouter(prefix="/insurance-providers", tags=["insurance-providers"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:ivr_playbooks:read")
_WRITE = platform_require("platform:ivr_playbooks:write")


class CreateProviderRequest(BaseModel):
    name: str
    working_hour_start: time | None = None
    working_hour_end: time | None = None
    # A mis-cased status would silently drop the provider from every status == "active"
    # lookup (e.g. the Voice Lab picker), so only the two known values are admitted.
    status: Literal["active", "inactive"] = "active"


class ProviderSummary(BaseModel):
    id: UUID
    name: str
    working_hour_start: time | None
    working_hour_end: time | None
    status: str
    created_at: datetime


def _summary(provider: InsuranceProvider) -> ProviderSummary:
    return ProviderSummary(
        id=provider.id,
        name=provider.name,
        working_hour_start=provider.working_hour_start,
        working_hour_end=provider.working_hour_end,
        status=provider.status,
        created_at=provider.created_at,
    )


@router.get(
    "",
    response_model=ResponseModel[list[ProviderSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_providers(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[ProviderSummary]]:
    rows = (
        await session.execute(select(InsuranceProvider).order_by(InsuranceProvider.name))
    ).scalars()
    return ok([_summary(p) for p in rows])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ResponseModel[ProviderSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def create_provider(
    body: CreateProviderRequest,
    request: Request,
    session: PlatformSession,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[ProviderSummary]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    provider = InsuranceProvider(
        name=body.name,
        working_hour_start=body.working_hour_start,
        working_hour_end=body.working_hour_end,
        status=body.status,
    )
    session.add(provider)
    await session.flush()  # populates provider.id / created_at
    return ok(_summary(provider))
