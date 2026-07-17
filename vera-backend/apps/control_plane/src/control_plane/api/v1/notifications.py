"""Login-session notification SSE: user-scoped realtime alerts (intervention
needed today; other event types later) over the per-tenant notification stream.

One connection per logged-in user for the whole session. Filtering is
server-side: the connection forwards only notifications addressed to this user
(owner-only alerts for unpublished calls) or tenant-wide ones (published /
ownerless calls) — the same owner-or-published visibility rule as the call
surfaces. Requires a tenant session + calls:read (v1 notifications are all
call-related). The stream tails from "now": current state is always recovered
from the REST API on (re)connect; this pipe is an accelerant, never the record.
"""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.common import Audit
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, get_resolver
from control_plane.deps import current_identity, get_notification_service, get_sessionmaker
from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from control_plane.request_context import current_request_id
from control_plane.sse import SSE_KEEPALIVE_FRAME
from vera_core.audit import AuditRecord
from vera_core.db.rls import tenant_session
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import AccountType
from vera_core.notifications import Notification, NotificationAudience, NotificationService

router = APIRouter(tags=["notifications"])


def delivers_to(audience: NotificationAudience, user_id: UUID) -> bool:
    """Server-side audience filter. Tenant-wide events reach every connection on
    this stream (the connect gate already required calls:read); user events only
    their addressee."""
    if audience.kind == "tenant":
        return True
    return audience.user_id == str(user_id)


async def notification_frames(
    items: AsyncIterator[tuple[str, Notification] | None], *, user_id: UUID
) -> AsyncIterator[str]:
    """Frame addressed notifications as SSE; idle ticks become keepalive comments
    (same proxy-timeout reasoning as frames_with_keepalive — filtered-out events
    produce no frame, so ticks are the only idle bytes)."""
    async for item in items:
        if item is None:
            yield SSE_KEEPALIVE_FRAME
            continue
        entry_id, notification = item
        if delivers_to(notification.audience, user_id):
            yield f"id: {entry_id}\ndata: {notification.model_dump_json()}\n\n"


@router.get("/notifications/stream")
async def stream_notifications(
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Audit,
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> StreamingResponse:
    """Authorization runs in a short-lived tenant session released before
    streaming (an SSE must not pin a DB connection — mirrors stream_call_events).
    The folded authz+access audit record below mirrors _authorize_call_read's
    SSE exception to the emit-helper rule (see control_plane/CLAUDE.md)."""
    if identity.account_type is not AccountType.TENANT or identity.tenant_id is None:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="notifications require a tenant session"
        )
    tenant_id = identity.tenant_id
    async with tenant_session(sessionmaker, tenant_id) as session:
        user_id, permissions = await resolver.effective_permissions(
            session, tenant_id, identity.user_id
        )
    allowed = "calls:read" in permissions
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="notifications",
            resource_id="stream",
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed or user_id is None:
        # user_id is None only when the resolver found no active user row, which
        # always comes back with an empty permission set — so this is really the
        # same "missing permission" case, just narrowing the type for the stream.
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )
    return StreamingResponse(
        notification_frames(service.tail(tenant_id), user_id=user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
