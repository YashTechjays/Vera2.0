"""Platform (SUPER_ADMIN) prompt-authoring catalog routes.

The prompt / prompt_version catalog is GLOBAL (no tenant_id, no RLS) and curated by
a platform operator. Authorization is platform_require (account_type='platform' +
dedicated platform:prompts:read / platform:prompts:write grants); no tenant context.
Versions are immutable — each save is a new draft; publishing promotes one and demotes
the prior published (uq_prompt_version_published_per_prompt enforces one published per
prompt).
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import client_ip, get_auth_audit, platform_scoped_session
from control_plane.exceptions import (
    ConflictError,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuthAuditRecord, AuthAuditSink
from vera_core.models import FormSchema, Prompt, PromptVersion, SchemaVersion
from vera_core.models.enums import AuthEvent, VersionStatus

router = APIRouter(prefix="/prompts", tags=["prompts"])

AuthAudit = Annotated[AuthAuditSink, Depends(get_auth_audit)]

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:prompts:read")
_WRITE = platform_require("platform:prompts:write")


class CreateDraftRequest(BaseModel):
    composite_json: dict[str, Any]


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
    # Outer join, not N+1: uq_prompt_version_published_per_prompt guarantees at
    # most one published row per prompt, so the join can't fan out.
    rows = (
        await session.execute(
            select(Prompt.id, Prompt.name, FormSchema.insurance_type, PromptVersion.version)
            .join(FormSchema, Prompt.schema_id == FormSchema.id)
            .outerjoin(
                PromptVersion,
                (PromptVersion.prompt_id == Prompt.id)
                & (PromptVersion.status == VersionStatus.PUBLISHED),
            )
            .order_by(Prompt.name)
        )
    ).all()
    return ok(
        [
            PromptSummary(
                id=row.id,
                name=row.name,
                insurance_type=row.insurance_type,
                published_version=row.version,
            )
            for row in rows
        ]
    )


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


@router.post(
    "/{prompt_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=ResponseModel[PromptVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def create_draft(
    prompt_id: UUID,
    body: CreateDraftRequest,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PromptVersionDetail]:
    prompt = await _require_prompt(session, prompt_id)
    published_schema_id = (
        await session.execute(
            select(SchemaVersion.id).where(
                SchemaVersion.schema_id == prompt.schema_id,
                SchemaVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
    if published_schema_id is None:
        raise ConflictError(message="no published schema to bind the prompt to")
    max_version = (
        await session.execute(
            select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt.id)
        )
    ).scalar()
    draft = PromptVersion(
        prompt_id=prompt.id,
        schema_version_id=published_schema_id,
        version=(max_version or 0) + 1,
        composite_json=body.composite_json,
        status=VersionStatus.DRAFT,
    )
    session.add(draft)
    try:
        await session.flush()
    except IntegrityError as exc:
        # A concurrent create raced the (prompt_id, version) unique constraint —
        # both computed the same next version. Surface a retryable 409, not a 500.
        raise ConflictError(message="version changed concurrently, please retry") from exc
    await audit.emit(
        AuthAuditRecord(
            tenant_id=None,
            app_user_id=caller.user_id,
            event_type=AuthEvent.PROMPT_VERSION_CREATED.value,
            ip_address=client_ip(request),
            meta={"prompt_id": str(prompt.id), "version": draft.version},
        )
    )
    return ok(_detail(draft))


@router.post(
    "/{prompt_id}/versions/{version_id}/publish",
    response_model=ResponseModel[PromptVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def publish_version(
    prompt_id: UUID,
    version_id: UUID,
    request: Request,
    session: PlatformSession,
    audit: AuthAudit,
    caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PromptVersionDetail]:
    target = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id, PromptVersion.prompt_id == prompt_id
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError(message="unknown prompt version")
    if target.status == VersionStatus.PUBLISHED:
        return ok(_detail(target))  # idempotent no-op
    current = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt_id,
                PromptVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
    if current is not None:
        # Demote first to free uq_prompt_version_published_per_prompt before publishing.
        current.status = VersionStatus.DRAFT
        await session.flush()
    target.status = VersionStatus.PUBLISHED
    try:
        await session.flush()
    except IntegrityError as exc:
        # A concurrent publish raced uq_prompt_version_published_per_prompt. The
        # transaction rolls back (no demote persists); ask the caller to retry.
        raise ConflictError(
            message="another version was published concurrently, please retry"
        ) from exc
    await audit.emit(
        AuthAuditRecord(
            tenant_id=None,
            app_user_id=caller.user_id,
            event_type=AuthEvent.PROMPT_VERSION_PUBLISHED.value,
            ip_address=client_ip(request),
            meta={
                "prompt_id": str(prompt_id),
                "version": target.version,
                "demoted_version": current.version if current else None,
            },
        )
    )
    return ok(_detail(target))
