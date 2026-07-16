"""Step 3 of the authz chain: require(permission, scope).

Effective permissions are resolved server-side from the RBAC tables
(user_roles -> role_permissions -> permissions, yielding permission codes)
inside the request's tenant-scoped session — RLS already constrains every row
touched. Results are cached per (tenant, subject) with a short TTL;
role/user-role write paths must call PermissionResolver.invalidate.

Every decision — allow or deny — lands in the audit log.
"""

import enum
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.permission_cache import PermissionCache
from control_plane.deps import (
    client_ip,
    current_elevation,
    current_identity,
    current_tenant_id,
    get_audit,
    get_auth_audit,
    platform_scoped_session,
    tenant_scoped_session,
)
from control_plane.request_context import current_request_id
from vera_core.audit import AuditRecord, AuditSink, AuthAuditSink, emit_auth_event
from vera_core.models import AppUser, Permission, RolePermission, UserRole
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import AuthEvent


class ResourceScope(enum.StrEnum):
    """How far the permission must reach. TENANT is the only scope and means a
    tenant-wide grant. It is an API concept, not a column — narrower typed
    scopes (team/queue) will arrive with those features."""

    TENANT = "tenant"


class PermissionResolver:
    def __init__(self, cache: PermissionCache) -> None:
        self._cache = cache

    async def effective_permissions(
        self, session: AsyncSession, tenant_id: UUID | None, user_id: UUID
    ) -> tuple[UUID | None, frozenset[str]]:
        """Return (user_id, permission codes) for the caller's grants. Keyed on
        app_user.id (the session carries it) — password users have no gcip_uid. The
        session must already be scoped: a tenant-scoped session resolves a tenant
        user's grants, a platform session (`tenant_id is None`) resolves a
        SUPER_ADMIN's global grants. RLS does the scoping, so a mismatched id
        resolves to no row. The cache keys on `tenant_id` directly — None is the
        platform scope.

        A cache hit vouches for the active-status check too (entries are written
        only after an RLS-scoped resolution of an active user), so it skips the DB
        entirely. The staleness window is the cache TTL, same as for revoked roles;
        the deactivate path calls `invalidate` to close it immediately."""
        cache_key = str(user_id)
        cached = await self._cache.get(tenant_id, cache_key)
        if cached is not None:
            return user_id, cached

        user = (
            await session.execute(
                select(AppUser).where(AppUser.id == user_id, AppUser.status == "active")
            )
        ).scalar_one_or_none()
        if user is None:
            return None, frozenset()

        rows = await session.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.app_user_id == user.id)
        )
        permissions = frozenset(rows.scalars())
        await self._cache.set(tenant_id, cache_key, permissions)
        return user.id, permissions

    async def invalidate(self, tenant_id: UUID | None, user_id: UUID) -> None:
        await self._cache.invalidate(tenant_id, str(user_id))


def get_resolver(request: Request) -> PermissionResolver:
    resolver: PermissionResolver = request.app.state.permission_resolver
    return resolver


async def emit_authz_audit(
    audit: AuditSink,
    request: Request,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    actor_label: str,
    permission: str,
    allowed: bool,
    scope: ResourceScope = ResourceScope.TENANT,
) -> None:
    """Standard endpoint authz allow/deny audit — the shape require() writes, so an
    endpoint that checks a permission itself doesn't drop reason / elevation_session_id."""
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=actor_label,
            event_type=AuditEvent.AUTHZ_ALLOW.value if allowed else AuditEvent.AUTHZ_DENY.value,
            resource_type="endpoint",
            resource_id=request.url.path,
            permission_key=permission,
            decision="allow" if allowed else "deny",
            reason="" if allowed else ("unknown user" if user_id is None else "not granted"),
            request_id=current_request_id(request),
            detail={"scope": scope.value},
            elevation_session_id=current_elevation(request),
        )
    )


def require(permission: str, scope: ResourceScope = ResourceScope.TENANT) -> Any:
    """Dependency factory: 403 unless the caller holds `permission` at `scope`
    in the tenant-context-resolved tenant. Audits allow AND deny."""

    async def dependency(
        request: Request,
        identity: Annotated[VerifiedIdentity, Depends(current_identity)],
        tenant_id: Annotated[UUID, Depends(current_tenant_id)],
        session: Annotated[AsyncSession, Depends(tenant_scoped_session)],
        resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    ) -> VerifiedIdentity:
        user_id, permissions = await resolver.effective_permissions(
            session, tenant_id, identity.user_id
        )
        allowed = permission in permissions
        await emit_authz_audit(
            get_audit(request),
            request,
            tenant_id=tenant_id,
            user_id=user_id,
            actor_label=identity.email or identity.subject,
            permission=permission,
            allowed=allowed,
            scope=scope,
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing permission {permission}",
            )
        return identity

    return Depends(dependency)


def platform_require(permission: str) -> Any:
    """Like `require`, but for /platform routes: there is no tenant context and no
    tenant GUC. Permissions resolve over a platform session (global SUPER_ADMIN
    grants), so a tenant user — whose grants are tenant-scoped — never matches and
    is denied. Allow AND deny are audited to the auth log (the PHI audit_log is
    tenant-scoped and can't hold a platform row); ADR-0006 §A/§B."""

    async def dependency(
        request: Request,
        identity: Annotated[VerifiedIdentity, Depends(current_identity)],
        session: Annotated[AsyncSession, Depends(platform_scoped_session)],
        resolver: Annotated[PermissionResolver, Depends(get_resolver)],
        auth_audit: Annotated[AuthAuditSink, Depends(get_auth_audit)],
    ) -> VerifiedIdentity:
        _resolved, permissions = await resolver.effective_permissions(
            session, None, identity.user_id
        )
        allowed = permission in permissions
        await emit_auth_event(
            auth_audit,
            tenant_id=None,
            event=AuthEvent.AUTHZ_ALLOW if allowed else AuthEvent.AUTHZ_DENY,
            ip=client_ip(request),
            # The caller per the verified token — recorded even when they hold no
            # platform grant (a tenant user is invisible to the platform session).
            user_id=identity.user_id,
            meta={
                "permission": permission,
                "path": request.url.path,
                "decision": "allow" if allowed else "deny",
                **({} if allowed else {"reason": "not granted"}),
            },
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing permission {permission}",
            )
        return identity

    return Depends(dependency)
