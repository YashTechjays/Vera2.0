"""Shared FastAPI dependency aliases + the auth-audit emit seam for the v1
tenant-admin routers (users, roles, providers, api_keys).

Centralizing the `Annotated[..., Depends(...)]` aliases and the `AuthAuditRecord`
construction keeps the routers thin and gives the auth-audit shape a single home —
so adding a field or changing the metadata contract is one edit, not many.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.invitations import InvitationStore
from control_plane.auth.rbac import PermissionResolver, get_resolver
from control_plane.deps import (
    current_tenant_id,
    get_auth_audit,
    get_call_plan_store,
    get_email_sender,
    get_invitation_store,
    get_kms,
    get_livekit,
    get_settings_state,
    self_scoped_session,
    tenant_scoped_session,
)
from control_plane.email import EmailSender
from vera_core.audit import AuthAuditRecord, AuthAuditSink
from vera_core.callplan import CallPlanStore
from vera_core.config import Settings
from vera_core.config.kms import KeyManagementService
from vera_core.models import Permission, RolePermission
from vera_core.models.enums import AuthEvent

if TYPE_CHECKING:
    from control_plane.livekit_gateway import LiveKitGateway

# Platform-tier permissions are namespaced `platform:*` (rbac_defaults). They must
# never reach the tenant tier — not in a tenant-owned custom role, not in any grant
# to a tenant user. This is the one invariant; it is enforced at EVERY tenant write
# seam (create_role, assign_role, invite_user), never just one.
PLATFORM_PERMISSION_PREFIX = "platform:"


def is_platform_permission(code: str) -> bool:
    return code.startswith(PLATFORM_PERMISSION_PREFIX)


# Request-time dependency aliases shared across the admin routers.
TenantSession = Annotated[AsyncSession, Depends(tenant_scoped_session)]
SelfScopedSession = Annotated[AsyncSession, Depends(self_scoped_session)]
TenantId = Annotated[UUID, Depends(current_tenant_id)]
AuthAudit = Annotated[AuthAuditSink, Depends(get_auth_audit)]
AppSettings = Annotated[Settings, Depends(get_settings_state)]
Invites = Annotated[InvitationStore, Depends(get_invitation_store)]
Email = Annotated[EmailSender, Depends(get_email_sender)]
Resolver = Annotated[PermissionResolver, Depends(get_resolver)]
LiveKit = Annotated["LiveKitGateway", Depends(get_livekit)]
Kms = Annotated[KeyManagementService, Depends(get_kms)]
CallPlans = Annotated[CallPlanStore, Depends(get_call_plan_store)]


async def emit_auth_event(
    sink: AuthAuditSink,
    *,
    tenant_id: UUID | None,
    event: AuthEvent,
    ip: str | None,
    user_id: UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Write one authN/Z event to the auth audit log. The single construction
    point for `AuthAuditRecord` across the auth + admin routers."""
    await sink.emit(
        AuthAuditRecord(
            tenant_id=tenant_id,
            app_user_id=user_id,
            event_type=event.value,
            ip_address=ip,
            meta=meta or {},
        )
    )


async def roles_grant_platform_permission(session: AsyncSession, role_ids: Iterable[UUID]) -> bool:
    """True if ANY of `role_ids` holds a `platform:*` permission — such roles are
    platform-tier (e.g. the global SUPER_ADMIN) and must not be granted at the
    tenant level. One query; used by both `assign_role` and `invite_user`."""
    ids = list(role_ids)
    if not ids:
        return False
    row = (
        await session.execute(
            select(Permission.id)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(
                RolePermission.role_id.in_(ids),
                Permission.code.like(f"{PLATFORM_PERMISSION_PREFIX}%"),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None
