"""Platform (SUPER_ADMIN) prompt-authoring catalog routes.

The prompt / prompt_version catalog is GLOBAL (no tenant_id, no RLS) and curated by
a platform operator. Authorization is platform_require (account_type='platform' + the
reused platform:elevations:read grant); no tenant context. Versions are immutable —
each save is a new draft; publishing promotes one and demotes the prior published
(uq_prompt_version_published_per_prompt enforces one published per prompt).
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, status  # noqa: F401  -- status used in Task 2 POST routes
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import platform_scoped_session
from control_plane.exceptions import (
    ConflictError,  # noqa: F401  -- used in Task 2 (create-draft / conflict handling)
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.models import FormSchema, Prompt, PromptVersion
from vera_core.models.enums import VersionStatus

router = APIRouter(prefix="/prompts", tags=["prompts"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:elevations:read")


class PromptSummary(BaseModel):
    id: UUID
    name: str
    insurance_type: str
    published_version: int | None


class PromptVersionSummary(BaseModel):
    id: UUID
    version: int
    status: str
    created_at: datetime


class PromptVersionDetail(BaseModel):
    id: UUID
    version: int
    status: str
    created_at: datetime
    composite_json: dict[str, Any]


def _detail(v: PromptVersion) -> PromptVersionDetail:
    return PromptVersionDetail(
        id=v.id,
        version=v.version,
        status=v.status,
        created_at=v.created_at,
        composite_json=v.composite_json,
    )


async def _require_prompt(session: AsyncSession, prompt_id: UUID) -> Prompt:
    prompt = (
        await session.execute(select(Prompt).where(Prompt.id == prompt_id))
    ).scalar_one_or_none()
    if prompt is None:
        raise NotFoundError(message="unknown prompt")
    return prompt


@router.get(
    "",
    response_model=ResponseModel[list[PromptSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_prompts(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[PromptSummary]]:
    rows = (
        await session.execute(
            select(Prompt.id, Prompt.name, FormSchema.insurance_type)
            .join(FormSchema, Prompt.schema_id == FormSchema.id)
            .order_by(Prompt.name)
        )
    ).all()
    summaries: list[PromptSummary] = []
    for row in rows:
        published_version = (
            await session.execute(
                select(PromptVersion.version).where(
                    PromptVersion.prompt_id == row.id,
                    PromptVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
        summaries.append(
            PromptSummary(
                id=row.id,
                name=row.name,
                insurance_type=row.insurance_type,
                published_version=published_version,
            )
        )
    return ok(summaries)


@router.get(
    "/{prompt_id}/versions",
    response_model=ResponseModel[list[PromptVersionSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def list_versions(
    prompt_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[PromptVersionSummary]]:
    await _require_prompt(session, prompt_id)
    rows = (
        await session.execute(
            select(
                PromptVersion.id,
                PromptVersion.version,
                PromptVersion.status,
                PromptVersion.created_at,
            )
            .where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.version.desc())
        )
    ).all()
    return ok(
        [
            PromptVersionSummary(
                id=r.id, version=r.version, status=r.status, created_at=r.created_at
            )
            for r in rows
        ]
    )


@router.get(
    "/{prompt_id}/versions/{version_id}",
    response_model=ResponseModel[PromptVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def get_version(
    prompt_id: UUID,
    version_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptVersionDetail]:
    version = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id, PromptVersion.prompt_id == prompt_id
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError(message="unknown prompt version")
    return ok(_detail(version))
