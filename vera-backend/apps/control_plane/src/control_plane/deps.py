"""FastAPI dependency plumbing.

The per-request authz chain composes in this fixed order — each step depends
on the previous one, so FastAPI cannot run them out of order:

    current_identity (401)  ->  tenant_context (403)  ->  require(permission) (403)

DB access goes through tenant_scoped_session, which opens the request
transaction and applies SET LOCAL app.tenant_id from the VERIFIED identity
(never from client input) — that's the RLS backstop underneath the chain.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth.elevation import active_grant_for_operator
from control_plane.auth.identity import InvalidTokenError, TokenVerifier, VerifiedIdentity
from control_plane.auth.invitations import InvitationStore
from control_plane.auth.session import SessionStore
from control_plane.email import EmailSender
from control_plane.idempotency import IdempotencyStore
from vera_core.audit import AuditSink, AuthAuditSink
from vera_core.call_plan import CallPlanStore
from vera_core.config import Settings
from vera_core.config.kms import KeyManagementService
from vera_core.db import elevated_session, platform_session, tenant_session
from vera_core.models.enums import AccountType
from vera_core.transcript import TranscriptService

if TYPE_CHECKING:
    from control_plane.livekit_gateway import LiveKitGateway

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class TenantContext:
    """The operating tenant for a request, derived from the verified session.

    `tenant_id` is always set — it is either the tenant user's home tenant or the
    platform operator's elevation target. `elevation_grant_id` is non-None only for
    platform operators operating under an active elevation grant.
    """

    tenant_id: UUID
    elevation_grant_id: UUID | None


def client_ip(request: Request) -> str | None:
    """Best-effort caller IP for auth-audit rows (never trusted for authz)."""
    return request.client.host if request.client is not None else None


def current_elevation(request: Request) -> UUID | None:
    """The active elevation grant id resolved earlier this request (by the tenant
    context / scoped session), or None for an ordinary tenant request. Lets the audit
    seam stamp `audit_log.elevation_session_id` without re-querying."""
    grant_id: UUID | None = getattr(request.state, "vera_elevation", None)
    return grant_id


def get_settings_state(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


def get_kms(request: Request) -> KeyManagementService:
    kms: KeyManagementService = request.app.state.kms
    return kms


def get_livekit(request: Request) -> LiveKitGateway:
    gw: LiveKitGateway | None = request.app.state.livekit
    if gw is None:
        raise RuntimeError("LiveKit gateway not configured (set VERA_LIVEKIT_URL)")
    return gw


def get_transcript_service(request: Request) -> TranscriptService:
    service: TranscriptService = request.app.state.transcript_service
    return service


def get_call_plan_store(request: Request) -> CallPlanStore:
    store: CallPlanStore = request.app.state.call_plan_store
    return store


def get_auth_audit(request: Request) -> AuthAuditSink:
    sink: AuthAuditSink = request.app.state.auth_audit
    return sink


def get_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return sessionmaker


def get_audit(request: Request) -> AuditSink:
    audit: AuditSink = request.app.state.audit
    return audit


def get_idempotency_store(request: Request) -> IdempotencyStore:
    store: IdempotencyStore = request.app.state.idempotency
    return store


def get_email_sender(request: Request) -> EmailSender:
    sender: EmailSender = request.app.state.email_sender
    return sender


def get_invitation_store(request: Request) -> InvitationStore:
    store: InvitationStore = request.app.state.invitation_store
    return store


async def current_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
) -> VerifiedIdentity:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await verifier.verify(credentials.credentials)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def tenant_context(
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> TenantContext:
    """Return the operating tenant from the verified session.

    TENANT users: pins to their own tenant_id (no DB hit).
    PLATFORM operators: looks up their single active elevation grant; 403 if none.
    Invariant mismatches (wrong nullability vs account_type) raise 401 fail-closed.

    On the platform/elevated path, stamps `request.state.vera_elevation = grant.id`
    so that `current_elevation()` and the audit seam agree without re-querying.
    """
    if identity.account_type is AccountType.TENANT:
        if identity.tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="malformed session",
            )
        return TenantContext(identity.tenant_id, None)

    # PLATFORM operator path
    if identity.tenant_id is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed session",
        )
    async with sessionmaker() as session:
        grant = await active_grant_for_operator(session, operator=identity.user_id)
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no active elevation for tenant",
        )
    request.state.vera_elevation = grant.id
    return TenantContext(grant.target_tenant_id, grant.id)


async def current_tenant_id(ctx: Annotated[TenantContext, Depends(tenant_context)]) -> UUID:
    """The verified operating tenant (from the session, not the URL). Thin accessor so
    routes that only need the tenant id depend on this instead of destructuring TenantContext."""
    return ctx.tenant_id


async def tenant_scoped_session(
    ctx: Annotated[TenantContext, Depends(tenant_context)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> AsyncGenerator[AsyncSession]:
    """Request-scoped session+transaction pinned to the tenant (SET LOCAL
    app.tenant_id). Commits on success, rolls back on exception.

    A tenant user pins their OWN verified tenant (never client input). A platform
    operator with an active elevation grant opens an `elevated_session` (tenant GUC +
    platform flag); tenant_context already raised 403 if no grant exists."""
    if ctx.elevation_grant_id is None:
        async with tenant_session(sessionmaker, ctx.tenant_id) as session:
            yield session
    else:
        async with elevated_session(sessionmaker, ctx.tenant_id) as session:
            yield session


async def platform_scoped_session(
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> AsyncGenerator[AsyncSession]:
    """A no-tenant session for /platform routes: app.platform='on', no tenant GUC,
    so only global (NULL-tenant) catalog/identity rows resolve. A tenant user's
    grants never match here, so platform RBAC naturally denies them."""
    async with platform_session(sessionmaker) as session:
        yield session


async def self_scoped_session(
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> AsyncGenerator[AsyncSession]:
    """RLS scope for a caller reading their OWN data (no tenant in the URL): a tenant
    user pins their own verified tenant; a platform operator gets the no-GUC platform
    session that resolves global/SUPER_ADMIN rows. No elevation, no slug — the scope
    comes from the verified identity, not request input."""
    if identity.account_type is AccountType.TENANT:
        if identity.tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="malformed session",
            )
        async with tenant_session(sessionmaker, identity.tenant_id) as session:
            yield session
    else:
        async with platform_session(sessionmaker) as session:
            yield session
