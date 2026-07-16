"""Platform (SUPER_ADMIN) insurance-provider catalog routes.

The insurance_provider catalog is GLOBAL (no tenant_id, no RLS, no PHI) and curated by a
platform operator — reference data shared across tenants that IVR playbooks attach to.
Authorization is platform_require (account_type='platform' + platform:insurance_providers
grants); no tenant context. Mirrors api/v1/ivr_playbooks.py.
"""

from datetime import datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import AppSettings
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import get_idempotency_store, platform_scoped_session
from control_plane.exceptions import (
    ConflictError,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.idempotency import (
    PLATFORM_IDEM_SCOPE,
    claim_or_conflict,
    require_idempotency_key,
)
from control_plane.responses import ResponseModel, ok
from vera_core.models import InsuranceProvider
from vera_core.models.enums import ProviderStatus

router = APIRouter(prefix="/insurance-providers", tags=["insurance-providers"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:insurance_providers:read")
_WRITE = platform_require("platform:insurance_providers:write")

# Trim surrounding whitespace and reject an empty/whitespace-only name at the edge.
_ProviderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreateProviderRequest(BaseModel):
    name: _ProviderName
    working_hour_start: time | None = None
    working_hour_end: time | None = None
    # A mis-cased status would silently drop the provider from every status == "active"
    # lookup (e.g. the Voice Lab picker), so only the catalog values are admitted.
    status: ProviderStatus = ProviderStatus.ACTIVE


class UpdateProviderRequest(BaseModel):
    # All optional for PATCH. The nullable working-hour fields distinguish "omitted"
    # (left unchanged) from an explicit null (cleared) via model_fields_set; name and
    # status are non-null columns, so a provided null is treated as "leave unchanged".
    name: _ProviderName | None = None
    working_hour_start: time | None = None
    working_hour_end: time | None = None
    status: ProviderStatus | None = None


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


async def _require_provider(session: AsyncSession, provider_id: UUID) -> InsuranceProvider:
    provider = (
        await session.execute(select(InsuranceProvider).where(InsuranceProvider.id == provider_id))
    ).scalar_one_or_none()
    if provider is None:
        raise NotFoundError(message="unknown insurance provider")
    return provider


async def _flush_or_name_conflict(session: AsyncSession) -> None:
    """Flush, mapping an IntegrityError to a 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(message="an insurance provider with this name already exists") from exc


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


@router.get(
    "/{provider_id}",
    response_model=ResponseModel[ProviderSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def get_provider(
    provider_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[ProviderSummary]:
    return ok(_summary(await _require_provider(session, provider_id)))


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
    await _flush_or_name_conflict(session)  # populates provider.id / created_at
    return ok(_summary(provider))


@router.patch(
    "/{provider_id}",
    response_model=ResponseModel[ProviderSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def update_provider(
    provider_id: UUID,
    body: UpdateProviderRequest,
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
    provider = await _require_provider(session, provider_id)
    fields_set = body.model_fields_set
    if body.name is not None:
        provider.name = body.name
    # Working hours are nullable, so an explicit null clears them; only touch a field the
    # caller actually sent (fields_set) rather than overwriting with the default None.
    if "working_hour_start" in fields_set:
        provider.working_hour_start = body.working_hour_start
    if "working_hour_end" in fields_set:
        provider.working_hour_end = body.working_hour_end
    if body.status is not None:
        provider.status = body.status
    await _flush_or_name_conflict(session)
    # updated_at is onupdate=func.now(); refresh so _summary reads the DB-computed value
    # rather than triggering a lazy (sync-in-async) reload of the expired column.
    await session.refresh(provider)
    return ok(_summary(provider))


@router.delete(
    "/{provider_id}",
    response_model=ResponseModel[ProviderSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def delete_provider(
    provider_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[ProviderSummary]:
    # Soft delete: a provider owns IVR playbooks (FK ON DELETE CASCADE), so removing the row
    # would destroy them. Deactivating instead excludes it from active lookups (Voice Lab
    # picker, call routing) while preserving history; re-activate via PATCH status=active.
    # Idempotent — deleting an already-inactive provider is a no-op success.
    provider = await _require_provider(session, provider_id)
    provider.status = ProviderStatus.INACTIVE
    await session.flush()
    await session.refresh(provider)
    return ok(_summary(provider))
