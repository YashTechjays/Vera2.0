"""Platform (SUPER_ADMIN) read-only form-schema catalog routes.

The form_schema / schema_version catalog is GLOBAL (no tenant_id, no RLS, no
PHI) — reference data shared across tenants. Authorization is platform_require
(account_type='platform' + platform:form_schemas:read); no tenant context.
Read-only: schemas are authored via the DSL + seed pipeline, not this API.
Mirrors api/v1/insurance_providers.py.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import platform_scoped_session
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode, NotFoundError
from control_plane.responses import ResponseModel, ok
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import VersionStatus

router = APIRouter(prefix="/form-schemas", tags=["form-schemas"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:form_schemas:read")


class FormSchemaSummary(BaseModel):
    id: UUID
    name: str
    insurance_type: str
    # Version number of the single published version, if any.
    active_version: int | None
    version_count: int
    created_at: datetime


class SchemaVersionSummary(BaseModel):
    id: UUID
    version: int
    status: str
    published_at: datetime | None
    created_at: datetime


@router.get(
    "",
    response_model=ResponseModel[list[FormSchemaSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_form_schemas(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[FormSchemaSummary]]:
    schemas = (await session.execute(select(FormSchema).order_by(FormSchema.name))).scalars().all()
    stats = (
        await session.execute(
            select(
                SchemaVersion.schema_id,
                func.count().label("version_count"),
                func.max(
                    case((SchemaVersion.status == VersionStatus.PUBLISHED, SchemaVersion.version))
                ).label("active_version"),
            ).group_by(SchemaVersion.schema_id)
        )
    ).all()
    by_schema = {row.schema_id: row for row in stats}
    summaries: list[FormSchemaSummary] = []
    for s in schemas:
        row = by_schema.get(s.id)
        summaries.append(
            FormSchemaSummary(
                id=s.id,
                name=s.name,
                insurance_type=s.insurance_type,
                active_version=row.active_version if row is not None else None,
                version_count=row.version_count if row is not None else 0,
                created_at=s.created_at,
            )
        )
    return ok(summaries)


@router.get(
    "/{schema_id}/versions",
    response_model=ResponseModel[list[SchemaVersionSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
    ),
)
async def list_schema_versions(
    schema_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[SchemaVersionSummary]]:
    exists = (
        await session.execute(select(FormSchema.id).where(FormSchema.id == schema_id))
    ).scalar_one_or_none()
    if exists is None:
        raise NotFoundError(message="unknown form schema")
    versions = (
        (
            await session.execute(
                select(SchemaVersion)
                .where(SchemaVersion.schema_id == schema_id)
                .order_by(SchemaVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return ok(
        [
            SchemaVersionSummary(
                id=v.id,
                version=v.version,
                status=v.status,
                published_at=v.published_at,
                created_at=v.created_at,
            )
            for v in versions
        ]
    )
