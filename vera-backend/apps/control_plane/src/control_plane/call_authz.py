"""Call-visibility and publish-authorization rules shared across the call-scoped
routers (`api/v1/calls.py` — join-token, event stream, end; `api/v1/coaching.py` —
coach, on-demand-transcribe). Centralized so Intervene and Coaching can't drift
onto different authorization logic.
"""

from typing import Literal
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, emit_authz_audit
from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from vera_core.audit import AuditSink
from vera_core.models import Call


def call_hidden_from(call: Call, user_id: UUID | None) -> bool:
    """Whether *user_id* must NOT see this call (→ the same 404 as a missing row,
    so a private call is never revealed by enumeration).

    A non-owner sees it only when it is published or ownerless. Shared by
    join-token, the event stream, end, and coaching so the visibility gates
    never diverge.
    """
    if call.initiated_by_id == user_id:
        return False
    return call.initiated_by_id is not None and not call.published


async def authorize_publish(
    call: Call,
    tenant_id: UUID,
    caller_user_id: UUID,
    session: AsyncSession,
    resolver: PermissionResolver,
) -> tuple[bool, Literal["owner", "permission"]]:
    """Whether *caller_user_id* may publish audio on *call* — Intervene and Coaching
    share this rule: owning the call, OR holding `calls:intervene`. Returns
    (allowed, which rule decided it) — the caller folds the latter into its audit
    record so "granted via calls:intervene" is never claimed for an owner-only grant."""
    if call.initiated_by_id == caller_user_id:
        return True, "owner"
    _, permissions = await resolver.effective_permissions(session, tenant_id, caller_user_id)
    return "calls:intervene" in permissions, "permission"


async def authorize_or_403(
    call: Call,
    tenant_id: UUID,
    caller: VerifiedIdentity,
    session: AsyncSession,
    resolver: PermissionResolver,
    audit: AuditSink,
    request: Request,
    *,
    audit_log_allows: bool = True,
) -> None:
    """`authorize_publish` + the standard authz audit (same shape `require()`
    writes) + a 403 on denial — the full check-and-raise wrapper shared by
    join_token's intervene branch and both coaching endpoints, so they can't
    drift onto slightly different audit/error shapes.

    `audit_log_allows=False` skips the WORM `audit_log` write on a granted
    call (a denial is still logged either way — rare and security-relevant).
    Coaching passes this: its per-message trail already lives in
    `InterventionEvent`, and a WORM row per coaching message would be exactly
    the high-frequency noise that ledger is meant to avoid.
    """
    allowed, granted_via = await authorize_publish(
        call, tenant_id, caller.user_id, session, resolver
    )
    if not allowed or audit_log_allows:
        await emit_authz_audit(
            audit,
            request,
            tenant_id=tenant_id,
            user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            permission="calls:intervene",
            allowed=allowed,
            detail={"granted_via": granted_via},
        )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:intervene"
        )
