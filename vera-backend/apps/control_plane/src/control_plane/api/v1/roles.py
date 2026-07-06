"""Role administration (spec §4.1.1 RBAC) — a TENANT_ADMIN lists roles, defines
custom tenant roles, and assigns/revokes roles to users.

Gated by `roles:manage`. Writes run in the tenant-scoped session: the catalog RLS
policy lets a tenant read the global system roles but only write its own, and the
strict policy confines `user_role` to the tenant. Every assign/revoke invalidates
the effective-permission cache for the affected user (auth/rbac.py) and is audited.
"""

from collections.abc import Sequence
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from control_plane.api.v1.common import (
    AuthAudit,
    Resolver,
    TenantId,
    TenantSession,
    emit_auth_event,
    is_platform_permission,
    roles_grant_platform_permission,
)
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import client_ip
from control_plane.exceptions import (
    BadRequestError,
    ConflictError,
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
    description: str = Field(default="", max_length=2000)
    permission_ids: list[UUID] = Field(default_factory=list)


class UpdateRoleRequest(BaseModel):
    """PATCH semantics: a field left as None is not changed; `permission_ids`
    (when present) REPLACES the role's whole permission set."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    permission_ids: list[UUID] | None = None


class AssignRoleRequest(BaseModel):
    role_id: UUID


class PermissionResponse(BaseModel):
    id: UUID
    code: str
    description: str


class RoleDetailResponse(RoleResponse):
    permissions: list[PermissionResponse]


def _to_response(row: Role) -> RoleResponse:
    return RoleResponse(
        id=row.id, name=row.name, description=row.description, is_system=row.tenant_id is None
    )


async def _role_permissions(session: AsyncSession, role_id: UUID) -> list[Permission]:
    """All permissions granted to `role_id`, UNFILTERED — platform-tier codes included.
    Callers must pass this through `_visible_permissions`/`_to_detail` before rendering,
    never return it directly."""
    return list(
        (
            await session.execute(
                select(Permission)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role_id)
                .order_by(Permission.code)
            )
        )
        .scalars()
        .all()
    )


def _visible_permissions(permissions: Sequence[Permission]) -> list[PermissionResponse]:
    # Platform-tier codes are never shown to a tenant — they can't be granted here,
    # so they must not appear as options (GET /permissions) or on a role (detail).
    return [
        PermissionResponse(id=p.id, code=p.code, description=p.description)
        for p in permissions
        if not is_platform_permission(p.code)
    ]


def _to_detail(role: Role, permissions: list[Permission]) -> RoleDetailResponse:
    return RoleDetailResponse(
        **_to_response(role).model_dump(),
        permissions=_visible_permissions(permissions),
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
    rows = (await session.execute(select(Role).order_by(Role.name))).scalars().all()
    return ok([_to_response(r) for r in rows])


@router.get(
    "/roles/{role_id}",
    response_model=ResponseModel[RoleDetailResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def get_role(
    role_id: UUID,
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[RoleDetailResponse]:
    role = await _load_role(session, role_id)
    return ok(_to_detail(role, await _role_permissions(session, role_id)))


@router.patch(
    "/roles/{role_id}",
    response_model=ResponseModel[RoleDetailResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def update_role(
    role_id: UUID,
    body: UpdateRoleRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    resolver: Resolver,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[RoleDetailResponse]:
    role = await _load_role(session, role_id)
    # Explicit ownership check (spec): don't rely on RLS's silent 0-row update.
    # `tenant_id IS NULL` here means "global system role", not "platform caller".
    if role.tenant_id is None:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="system roles cannot be modified"
        )

    changed: list[str] = []
    if body.name is not None and body.name != role.name:
        role.name = body.name
        changed.append("name")
    if body.description is not None and body.description != role.description:
        role.description = body.description
        changed.append("description")

    if body.permission_ids is not None:
        permissions = await _resolve_grantable_permissions(session, body.permission_ids)
        links = (
            (await session.execute(select(RolePermission).where(RolePermission.role_id == role_id)))
            .scalars()
            .all()
        )
        if {link.permission_id for link in links} != {p.id for p in permissions}:
            for link in links:
                await session.delete(link)
            for permission in permissions:
                session.add(
                    RolePermission(
                        tenant_id=tenant_id, role_id=role_id, permission_id=permission.id
                    )
                )
            changed.append("permissions")

    try:
        await session.flush()
    except IntegrityError as exc:
        raise _conflict_or_raise(exc, "a role with that name already exists") from exc

    if "permissions" in changed:
        # The role's grants changed under live users — drop every holder's cached
        # permission set or they keep the old access until the cache TTL expires.
        holders = (
            (await session.execute(select(UserRole.app_user_id).where(UserRole.role_id == role_id)))
            .scalars()
            .all()
        )
        for holder_id in holders:
            await resolver.invalidate(tenant_id, holder_id)

    if changed:
        await emit_auth_event(
            audit,
            tenant_id=tenant_id,
            event=AuthEvent.ROLE_UPDATED,
            ip=client_ip(request),
            user_id=_caller.user_id,
            meta={"role_id": str(role_id), "changed": changed},
        )
    return ok(_to_detail(role, await _role_permissions(session, role_id)))


@router.get(
    "/permissions",
    response_model=ResponseModel[list[PermissionResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_permissions(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[list[PermissionResponse]]:
    # The permission catalog is global (no tenant_id, no RLS) and code-defined —
    # tenants get a read-only view.
    rows = (await session.execute(select(Permission).order_by(Permission.code))).scalars().all()
    return ok(_visible_permissions(rows))


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
    # Resolve permission ids and enforce the tenant-role grant guard (defense in depth:
    # platform-tier perms are also blocked at assignment, but stop them from entering here).
    permissions = await _resolve_grantable_permissions(session, body.permission_ids)

    role = Role(tenant_id=tenant_id, name=body.name, description=body.description)
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
        UserRole(
            tenant_id=tenant_id,
            app_user_id=user_id,
            role_id=body.role_id,
            granted_by=_caller.user_id,
            granted_at=func.now(),
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


@router.get(
    "/users/{user_id}/roles",
    response_model=ResponseModel[list[RoleResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_user_roles(
    user_id: UUID,
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[list[RoleResponse]]:
    if not await _user_in_tenant(session, user_id):
        raise NotFoundError(message="no such user in this tenant")
    rows = (
        (
            await session.execute(
                select(Role)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.app_user_id == user_id)
                .order_by(Role.name)
            )
        )
        .scalars()
        .all()
    )
    return ok([_to_response(r) for r in rows])


@router.delete(
    "/users/{user_id}/roles/{role_id}",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
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

    # Self-lockout guard (settled decision): a caller may not remove their own
    # LAST source of roles:manage — nobody in the tenant could manage roles and
    # the only recovery is platform break-glass elevation. Self-only by design.
    if user_id == _caller.user_id:
        other_source = (
            await session.execute(
                select(Permission.id)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(UserRole, UserRole.role_id == RolePermission.role_id)
                .where(
                    UserRole.app_user_id == user_id,
                    UserRole.role_id != role_id,
                    Permission.code == "roles:manage",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if other_source is None:
            raise ConflictError(message="you cannot remove your own last role-management role")

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


@router.delete(
    "/roles/{role_id}",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def delete_role(
    role_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    _caller: VerifiedIdentity = require("roles:manage"),
) -> ResponseModel[None]:
    role = await _load_role(session, role_id)
    if role.tenant_id is None:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="system roles cannot be deleted"
        )
    # DECISION: never cascade a delete over live grants. The admin revokes per
    # user first (each revoke is audited + cache-invalidating), so by the time
    # this runs there is no holder cache to invalidate.
    holder_count = (
        await session.execute(
            select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
        )
    ).scalar_one()
    if holder_count:
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message=f"{holder_count} user(s) still hold this role — remove it from them first",
            data={"holder_count": holder_count},
        )

    await session.delete(role)  # FK cascade clears role_permission links
    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.ROLE_DELETED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"role_id": str(role_id), "name": role.name},
    )
    return ok(None, message="Role deleted.")


async def _user_in_tenant(session: AsyncSession, user_id: UUID) -> bool:
    row = (
        await session.execute(select(AppUser.id).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    return row is not None


async def _role_visible(session: AsyncSession, role_id: UUID) -> bool:
    row = (await session.execute(select(Role.id).where(Role.id == role_id))).scalar_one_or_none()
    return row is not None


async def _load_role(session: AsyncSession, role_id: UUID) -> Role:
    """Load a role visible in the current tenant context, or 404. An unknown id — or
    another tenant's role hidden by RLS — resolves to no row and is rejected."""
    role = (await session.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if role is None:
        raise NotFoundError(message="no such role")
    return role


async def _resolve_grantable_permissions(
    session: AsyncSession, permission_ids: Sequence[UUID]
) -> list[Permission]:
    """Resolve ids against the global permission catalog, rejecting any unknown id
    (400) or platform-tier code (403 — a tenant role may never carry one). Shared by
    create_role and update_role so both apply the identical grant guard."""
    permissions = (
        (await session.execute(select(Permission).where(Permission.id.in_(permission_ids))))
        .scalars()
        .all()
    )
    if len(permissions) != len(set(permission_ids)):
        raise BadRequestError(message="unknown permission id")
    if any(is_platform_permission(p.code) for p in permissions):
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN,
            message="cannot grant a platform-tier permission to a tenant role",
        )
    return list(permissions)


def _conflict_or_raise(exc: IntegrityError, message: str) -> CustomAPIException:
    if getattr(exc.orig, "sqlstate", None) == "23505":  # unique_violation
        return CustomAPIException(DefaultExceptionCode.CONFLICT, message=message)
    return BadRequestError(message="request rejected")
