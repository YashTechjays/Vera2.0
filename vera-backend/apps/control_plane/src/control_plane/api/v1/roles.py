"""Role administration (spec §4.1.1 RBAC) — a TENANT_ADMIN lists roles, defines
custom tenant roles, and assigns/revokes roles to users.

Gated by `roles:manage`. Writes run in the tenant-scoped session: the catalog RLS
policy lets a tenant read the global system roles but only write its own, and the
strict policy confines `user_role` to the tenant. Every assign/revoke invalidates
the effective-permission cache for the affected user (auth/rbac.py) and is audited.
"""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import (
    PLATFORM_PERMISSION_PREFIX,
    AuthAudit,
    Resolver,
    TenantId,
    TenantSession,
    build_role_grant,
    emit_auth_event,
    is_platform_permission,
    roles_grant_platform_permission,
)
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import client_ip
from control_plane.exceptions import (
    BadRequestError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.models import AppUser, Permission, Role, RolePermission, UserRole
from vera_core.models.enums import AuthEvent

router = APIRouter(tags=["roles"])


class RoleResponse(BaseModel):
    id: UUID
    name: str
    description: str
    is_system: bool  # global template role (tenant_id IS NULL)


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    permission_ids: list[UUID] = Field(default_factory=list)


class AssignRoleRequest(BaseModel):
    role_id: UUID


def _to_response(row: Role) -> RoleResponse:
    return RoleResponse(
        id=row.id, name=row.name, description=row.description, is_system=row.tenant_id is None
    )


@router.get(
    "/roles",
    response_model=ResponseModel[list[RoleResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_roles(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[list[RoleResponse]]:
    # Catalog RLS returns the global system roles plus this tenant's custom roles.
    # Platform-tier roles (e.g. SUPER_ADMIN) are excluded via an anti-join against
    # role_permission/permission: a tenant can never assign one
    # (roles_grant_platform_permission blocks it at write time), so listing it
    # would just be a confusing, unusable option. One query, not a fetch-then-filter
    # round trip.
    platform_tier_roles = (
        select(RolePermission.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(Permission.code.like(f"{PLATFORM_PERMISSION_PREFIX}%"))
    )
    rows = (
        (
            await session.execute(
                select(Role).where(Role.id.not_in(platform_tier_roles)).order_by(Role.name)
            )
        )
        .scalars()
        .all()
    )
    return ok([_to_response(r) for r in rows])


@router.post(
    "/roles",
    response_model=ResponseModel[RoleResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def create_role(
    body: CreateRoleRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[RoleResponse]:
    # Resolve permission codes; reject any unknown id (permission is a global catalog).
    permissions = (
        (await session.execute(select(Permission).where(Permission.id.in_(body.permission_ids))))
        .scalars()
        .all()
    )
    if len(permissions) != len(set(body.permission_ids)):
        raise BadRequestError(message="unknown permission id")
    # A tenant role may never carry a platform-tier permission (defense in depth:
    # also blocked at assignment, but stop it from entering the role at all).
    if any(is_platform_permission(p.code) for p in permissions):
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN,
            message="cannot grant a platform-tier permission to a tenant role",
        )

    role = Role(tenant_id=tenant_id, name=body.name, description="")
    session.add(role)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _conflict_or_raise(exc, "a role with that name already exists") from exc
    for permission in permissions:
        session.add(
            RolePermission(tenant_id=tenant_id, role_id=role.id, permission_id=permission.id)
        )
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.ROLE_CREATED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"role_id": str(role.id), "name": body.name},
    )
    return ok(_to_response(role))


@router.post(
    "/users/{user_id}/roles",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def assign_role(
    user_id: UUID,
    body: AssignRoleRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    resolver: Resolver,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[None]:
    # Both the target user and the role must be visible in this tenant (RLS) — this
    # closes a cross-tenant grant: a user_id/role_id from another tenant resolves to
    # no row and is rejected, never silently linked.
    if not await _user_in_tenant(session, user_id):
        raise NotFoundError(message="no such user in this tenant")
    if not await _role_visible(session, body.role_id):
        raise NotFoundError(message="no such role")
    # A tenant may assign the global templates (TENANT_ADMIN/SUPERVISOR) and its own
    # custom roles, but NEVER a platform-privileged role — SUPER_ADMIN is granted only
    # by a platform operator. Keyed on the `platform:*` permission, not the role name,
    # so any future platform role is covered too.
    if await roles_grant_platform_permission(session, [body.role_id]):
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN,
            message="cannot assign a platform-privileged role",
        )

    session.add(
        build_role_grant(
            tenant_id=tenant_id,
            app_user_id=user_id,
            role_id=body.role_id,
            granted_by=_caller.user_id,
        )
    )
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _conflict_or_raise(exc, "user already has that role") from exc

    await resolver.invalidate(tenant_id, user_id)
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.ROLE_GRANT,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"target_user": str(user_id), "role_id": str(body.role_id)},
    )
    return ok(None, message="Role assigned.")


@router.delete(
    "/users/{user_id}/roles/{role_id}",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def revoke_role(
    user_id: UUID,
    role_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    resolver: Resolver,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[None]:
    assignment = (
        await session.execute(
            select(UserRole).where(UserRole.app_user_id == user_id, UserRole.role_id == role_id)
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError(message="user does not have that role")
    await session.delete(assignment)

    await resolver.invalidate(tenant_id, user_id)
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.ROLE_REVOKE,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"target_user": str(user_id), "role_id": str(role_id)},
    )
    return ok(None, message="Role revoked.")


async def _user_in_tenant(session: AsyncSession, user_id: UUID) -> bool:
    row = (
        await session.execute(select(AppUser.id).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    return row is not None


async def _role_visible(session: AsyncSession, role_id: UUID) -> bool:
    row = (await session.execute(select(Role.id).where(Role.id == role_id))).scalar_one_or_none()
    return row is not None


def _conflict_or_raise(exc: IntegrityError, message: str) -> CustomAPIException:
    if getattr(exc.orig, "sqlstate", None) == "23505":  # unique_violation
        return CustomAPIException(DefaultExceptionCode.CONFLICT, message=message)
    return BadRequestError(message="request rejected")
