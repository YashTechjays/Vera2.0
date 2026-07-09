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

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import platform_scoped_session
from control_plane.exceptions import (
    BadRequestError,
    ConflictError,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import (
    PromptDocument,
    RenderedPrompts,
    render_task_prompts,
    validate_prompt_document,
)
from vera_core.models import FormSchema, Prompt, PromptVersion, SchemaVersion
from vera_core.models.enums import VersionStatus

router = APIRouter(prefix="/prompts", tags=["prompts"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:prompts:read")
_WRITE = platform_require("platform:prompts:write")


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
    schema_version_id: UUID
    schema_version: int


class PromptVersionDetail(BaseModel):
    id: UUID
    version: int
    status: str
    created_at: datetime
    schema_version_id: UUID
    schema_version: int
    composite_json: dict[str, Any]


def _detail(v: PromptVersion, schema_version: int) -> PromptVersionDetail:
    return PromptVersionDetail(
        id=v.id,
        version=v.version,
        status=v.status,
        created_at=v.created_at,
        schema_version_id=v.schema_version_id,
        schema_version=schema_version,
        composite_json=v.composite_json,
    )


async def _schema_version_number(session: AsyncSession, schema_version_id: UUID) -> int:
    return (
        await session.execute(
            select(SchemaVersion.version).where(SchemaVersion.id == schema_version_id)
        )
    ).scalar_one()


async def _require_prompt(session: AsyncSession, prompt_id: UUID) -> Prompt:
    prompt = (
        await session.execute(select(Prompt).where(Prompt.id == prompt_id))
    ).scalar_one_or_none()
    if prompt is None:
        raise NotFoundError(message="unknown prompt")
    return prompt


async def _require_version(
    session: AsyncSession, prompt_id: UUID, version_id: UUID
) -> PromptVersion:
    version = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id, PromptVersion.prompt_id == prompt_id
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError(message="unknown prompt version")
    return version


async def _published_schema_version(session: AsyncSession, schema_id: UUID) -> SchemaVersion | None:
    return (
        await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == schema_id,
                SchemaVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()


async def _require_published_schema(
    session: AsyncSession, prompt_id: UUID, *, when_missing: str
) -> tuple[Prompt, SchemaVersion]:
    """The prompt plus its currently-published schema version. 404 if the prompt is
    unknown; 409 (with `when_missing`) if the schema has no published version yet."""
    prompt = await _require_prompt(session, prompt_id)
    schema_version = await _published_schema_version(session, prompt.schema_id)
    if schema_version is None:
        raise ConflictError(message=when_missing)
    return prompt, schema_version


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
                PromptVersion.schema_version_id,
                SchemaVersion.version.label("schema_version"),
            )
            .join(SchemaVersion, SchemaVersion.id == PromptVersion.schema_version_id)
            .where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.version.desc())
        )
    ).all()
    return ok(
        [
            PromptVersionSummary(
                id=r.id,
                version=r.version,
                status=r.status,
                created_at=r.created_at,
                schema_version_id=r.schema_version_id,
                schema_version=r.schema_version,
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
    version = await _require_version(session, prompt_id, version_id)
    schema_version = await _schema_version_number(session, version.schema_version_id)
    return ok(_detail(version, schema_version))


class PromptSchemaDetail(BaseModel):
    """The published schema version the next draft will pin. Platform mirror of
    patient_forms' SchemaVersionDetail — that route is tenant-gated, so a platform
    operator without an elevation grant cannot use it (spec 2026-07-09 §2.2)."""

    id: UUID
    schema_id: UUID
    version: int
    status: str
    insurance_type: str
    name: str
    document: dict[str, Any]


@router.get(
    "/{prompt_id}/schema",
    response_model=ResponseModel[PromptSchemaDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def get_prompt_schema(
    prompt_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptSchemaDetail]:
    """The editor's source for task text defaults + the placeholder namespace."""
    prompt, schema_version = await _require_published_schema(
        session, prompt_id, when_missing="no published schema version"
    )
    form_schema = (
        await session.execute(select(FormSchema).where(FormSchema.id == prompt.schema_id))
    ).scalar_one()
    return ok(
        PromptSchemaDetail(
            id=schema_version.id,
            schema_id=schema_version.schema_id,
            version=schema_version.version,
            status=schema_version.status,
            insurance_type=form_schema.insurance_type,
            name=form_schema.name,
            document=schema_version.schema_json,
        )
    )


@router.get(
    "/{prompt_id}/preview",
    response_model=ResponseModel[RenderedPrompts],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def preview_prompt(
    prompt_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
    version_id: UUID | None = None,
) -> ResponseModel[RenderedPrompts]:
    """Effective rendered prompts: the named version's document (or the published
    one; none → factory + no overrides) over the schema document it pins."""
    prompt = await _require_prompt(session, prompt_id)
    version: PromptVersion | None
    if version_id is not None:
        version = await _require_version(session, prompt.id, version_id)
    else:
        version = (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.prompt_id == prompt.id,
                    PromptVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
    schema_version: SchemaVersion | None
    if version is not None:
        schema_version = (
            await session.execute(
                select(SchemaVersion).where(SchemaVersion.id == version.schema_version_id)
            )
        ).scalar_one()
        prompt_doc = PromptDocument.model_validate(version.composite_json)
    else:
        schema_version = await _published_schema_version(session, prompt.schema_id)
        if schema_version is None:
            raise ConflictError(message="no published schema to render against")
        prompt_doc = None
    schema_doc = FormSchemaDoc.model_validate(schema_version.schema_json)
    return ok(render_task_prompts(schema_doc, prompt_doc))


class PromptPreview(BaseModel):
    """Stateless dry-run render. `errors` uses the exact save-time 400 strings
    (same validate_prompt_document); 200 even when non-empty because the renderer
    tolerates content errors and the editor polls this while typing (spec §2.3)."""

    errors: list[str]
    rendered: RenderedPrompts


@router.post(
    "/{prompt_id}/preview",
    response_model=ResponseModel[PromptPreview],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def preview_document(
    prompt_id: UUID,
    body: PromptDocument,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[PromptPreview]:
    """Render an unsaved document against the published schema; persist nothing.

    Read-gated: a dry run that mutates nothing (POST only because the document
    travels in the body). Deliberately NOT idempotency-gated — non-mutating."""
    _prompt, published_schema = await _require_published_schema(
        session, prompt_id, when_missing="no published schema to render against"
    )
    schema_doc = FormSchemaDoc.model_validate(published_schema.schema_json)
    return ok(
        PromptPreview(
            # Same joined, location-prefixed contract as create_draft's 400 message
            # below (e.g. "session.persona: ...") — parsed by the frontend via
            # vera-frontend/src/lib/prompts/document.ts parsePromptErrors.
            errors=validate_prompt_document(body, schema_doc),
            rendered=render_task_prompts(schema_doc, body),
        )
    )


@router.post(
    "/{prompt_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=ResponseModel[PromptVersionDetail],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.BAD_REQUEST,
    ),
)
async def create_draft(
    prompt_id: UUID,
    body: PromptDocument,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PromptVersionDetail]:
    prompt, published_schema = await _require_published_schema(
        session, prompt_id, when_missing="no published schema to bind the prompt to"
    )
    schema_doc = FormSchemaDoc.model_validate(published_schema.schema_json)
    content_errors = validate_prompt_document(body, schema_doc)
    if content_errors:
        # "; "-joined, location-prefixed (e.g. "session.persona: ...") — this exact
        # format is re-parsed by the frontend via
        # vera-frontend/src/lib/prompts/document.ts parsePromptErrors, so don't
        # reshape it without updating that parser too.
        raise BadRequestError(message="; ".join(content_errors))
    max_version = (
        await session.execute(
            select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt.id)
        )
    ).scalar()
    draft = PromptVersion(
        prompt_id=prompt.id,
        schema_version_id=published_schema.id,
        version=(max_version or 0) + 1,
        composite_json=body.model_dump(mode="json"),
        status=VersionStatus.DRAFT,
    )
    session.add(draft)
    try:
        await session.flush()
    except IntegrityError as exc:
        # A concurrent create raced the (prompt_id, version) unique constraint —
        # both computed the same next version. Surface a retryable 409, not a 500.
        raise ConflictError(message="version changed concurrently, please retry") from exc
    return ok(_detail(draft, published_schema.version))


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
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PromptVersionDetail]:
    target = await _require_version(session, prompt_id, version_id)
    schema_version = await _schema_version_number(session, target.schema_version_id)
    if target.status == VersionStatus.PUBLISHED:
        return ok(_detail(target, schema_version))  # idempotent no-op
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
    return ok(_detail(target, schema_version))
