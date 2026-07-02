"""Platform (SUPER_ADMIN) per-provider IVR playbook routes.

The ivr_playbook catalog is GLOBAL (no tenant_id, no RLS, no PHI) and curated by a platform
operator. A playbook is a structured, non-PHI navigation overlay (IvrPlaybookConfig) attached
to an insurance_provider; at call start the control plane resolves the provider's ACTIVE
playbook and injects it into dispatch metadata so the worker specializes the generic IVR
navigator. At most one active playbook per provider (uq_ivr_playbook_active_per_provider);
activating one demotes the prior active first — the demote-then-promote pattern from
prompts.py::publish_version. Authorization is platform_require; no tenant context.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import platform_scoped_session
from control_plane.exceptions import (
    ConflictError,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.models import InsuranceProvider, IvrPlaybook
from vera_core.schemas import IvrPlaybookConfig

router = APIRouter(prefix="/ivr-playbooks", tags=["ivr-playbooks"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:ivr_playbooks:read")
_WRITE = platform_require("platform:ivr_playbooks:write")

_ACTIVE = "active"
_INACTIVE = "inactive"


class CreatePlaybookRequest(BaseModel):
    provider_id: UUID
    instructions: IvrPlaybookConfig
    status: str = _ACTIVE


class UpdatePlaybookRequest(BaseModel):
    instructions: IvrPlaybookConfig | None = None
    status: str | None = None


class PlaybookSummary(BaseModel):
    id: UUID
    provider_id: UUID
    status: str
    created_at: datetime


class PlaybookDetail(BaseModel):
    id: UUID
    provider_id: UUID
    status: str
    instructions: IvrPlaybookConfig
    created_at: datetime
    updated_at: datetime


def _summary(pb: IvrPlaybook) -> PlaybookSummary:
    return PlaybookSummary(
        id=pb.id, provider_id=pb.provider_id, status=pb.status, created_at=pb.created_at
    )


def _detail(pb: IvrPlaybook) -> PlaybookDetail:
    return PlaybookDetail(
        id=pb.id,
        provider_id=pb.provider_id,
        status=pb.status,
        instructions=IvrPlaybookConfig.model_validate(pb.instructions),
        created_at=pb.created_at,
        updated_at=pb.updated_at,
    )


async def _require_playbook(session: AsyncSession, playbook_id: UUID) -> IvrPlaybook:
    pb = (
        await session.execute(select(IvrPlaybook).where(IvrPlaybook.id == playbook_id))
    ).scalar_one_or_none()
    if pb is None:
        raise NotFoundError(message="unknown ivr playbook")
    return pb


async def _demote_active(
    session: AsyncSession, provider_id: UUID, *, except_id: UUID | None = None
) -> None:
    """Demote any currently-active playbook for the provider so the partial unique index
    (one active per provider) frees up before another is activated."""
    stmt = select(IvrPlaybook).where(
        IvrPlaybook.provider_id == provider_id, IvrPlaybook.status == _ACTIVE
    )
    if except_id is not None:
        stmt = stmt.where(IvrPlaybook.id != except_id)
    for pb in (await session.execute(stmt)).scalars():
        pb.status = _INACTIVE
    await session.flush()


async def _flush_or_active_conflict(session: AsyncSession) -> None:
    """Flush, translating a raced uq_ivr_playbook_active_per_provider violation into a retryable
    409 (another active playbook for the provider won the race) instead of a 500."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            message="an active playbook already exists for this provider, please retry"
        ) from exc


@router.get(
    "",
    response_model=ResponseModel[list[PlaybookSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_playbooks(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
    provider_id: UUID | None = None,
) -> ResponseModel[list[PlaybookSummary]]:
    stmt = select(IvrPlaybook).order_by(IvrPlaybook.created_at.desc())
    if provider_id is not None:
        stmt = stmt.where(IvrPlaybook.provider_id == provider_id)
    rows = (await session.execute(stmt)).scalars()
    return ok([_summary(pb) for pb in rows])


@router.get(
    "/{playbook_id}",
    response_model=ResponseModel[PlaybookDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def get_playbook(
    playbook_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PlaybookDetail]:
    return ok(_detail(await _require_playbook(session, playbook_id)))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ResponseModel[PlaybookDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def create_playbook(
    body: CreatePlaybookRequest,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PlaybookDetail]:
    provider_exists = (
        await session.execute(
            select(InsuranceProvider.id).where(InsuranceProvider.id == body.provider_id)
        )
    ).scalar_one_or_none()
    if provider_exists is None:
        raise NotFoundError(message="unknown insurance provider")
    if body.status == _ACTIVE:
        await _demote_active(session, body.provider_id)
    pb = IvrPlaybook(
        provider_id=body.provider_id,
        instructions=body.instructions.model_dump(exclude_none=True),
        status=body.status,
    )
    session.add(pb)
    await _flush_or_active_conflict(session)
    return ok(_detail(pb))


@router.patch(
    "/{playbook_id}",
    response_model=ResponseModel[PlaybookDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def update_playbook(
    playbook_id: UUID,
    body: UpdatePlaybookRequest,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PlaybookDetail]:
    pb = await _require_playbook(session, playbook_id)
    new_status = body.status if body.status is not None else pb.status
    if new_status == _ACTIVE:
        await _demote_active(session, pb.provider_id, except_id=pb.id)
    if body.instructions is not None:
        pb.instructions = body.instructions.model_dump(exclude_none=True)
    pb.status = new_status
    await _flush_or_active_conflict(session)
    # updated_at is onupdate=func.now(); refresh so _detail reads the DB-computed value rather
    # than triggering a lazy (sync-in-async) reload of the expired column.
    await session.refresh(pb)
    return ok(_detail(pb))


@router.delete(
    "/{playbook_id}",
    response_model=ResponseModel[PlaybookDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def delete_playbook(
    playbook_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PlaybookDetail]:
    pb = await _require_playbook(session, playbook_id)
    detail = _detail(pb)
    await session.delete(pb)
    await session.flush()
    return ok(detail)
