"""Login / MFA endpoints (per tenant, provider-driven) + token-scoped self-session endpoints.

`POST /tenants/{tenant_slug}/auth/login` resolves the slug to a tenant id (via the
migration-0008 SECURITY DEFINER `resolve_tenant_by_slug`, since no RLS context exists
pre-auth) then opens a tenant-scoped (RLS) session, so a caller-supplied slug only ever
exposes that one tenant's rows — and the password gates everything. An unknown slug
returns the same uniform 401 as an unknown tenant. Success mints an opaque session
token (auth/session); if MFA is required it returns a short-lived challenge token, completed at
/mfa/verify. Outcomes within a resolved tenant are written to auth_audit_log;
failures return a uniform 401 (no user / provider enumeration).

MFA enrollment (/mfa/enroll, /mfa/activate) is authenticated; the TOTP seed is
envelope-encrypted (per-user DEK wrapped by the KMS) and the recovery codes are
bcrypt-hashed, both stored directly on the user_identity row (auth/mfa).

`GET /auth/me`, `POST /auth/logout`, and `POST /auth/session/keepalive` are token-scoped
self-session endpoints: they carry no tenant slug in the URL. Authentication is proved by
the bearer token alone (`current_identity`); scope is derived from the verified identity via
`self_scoped_session`. The caller can only operate on their own session.
"""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.common import (
    AppSettings,
    AuthAudit,
    Invites,
    Resolver,
    SelfScopedSession,
    TenantId,
)
from control_plane.auth import elevation, mfa
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.invitations import INVITE_MFA_NS, INVITE_NS
from control_plane.auth.password import (
    MAX_PASSWORD_BYTES,
    hash_password,
    verify_password_or_dummy,
)
from control_plane.auth.providers import resolve_login_provider
from control_plane.auth.session import MFA_ENROLL_NS, MFA_NS, SessionData, SessionStore
from control_plane.auth.tenant_slug import normalize_slug, resolve_tenant_id
from control_plane.deps import (
    client_ip,
    current_identity,
    get_kms,
    get_session_store,
    get_sessionmaker,
)
from control_plane.exceptions import (
    BadRequestError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    UnauthorizedError,
)
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuthAuditSink, emit_auth_event
from vera_core.config.kms import KeyManagementService
from vera_core.db import tenant_session
from vera_core.models import AppUser, Role, SsoProvider, UserIdentity, UserRole
from vera_core.models.enums import AccountType, AuthEvent, ProviderKind

router = APIRouter(tags=["auth"])

_bearer = HTTPBearer(auto_error=False)

Sessionmaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
Store = Annotated[SessionStore, Depends(get_session_store)]
KMS = Annotated[KeyManagementService, Depends(get_kms)]


# --- request/response models -------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    # `mfa` discriminates the next step:
    #   "none"   → fully authenticated; `session_token` is set.
    #   "verify" → enrolled user; POST `mfa_token` to /auth/mfa/verify.
    #   "enroll" → enforced-but-unenrolled; show `provisioning_uri`, then POST
    #              `mfa_token` to /auth/mfa/enroll-activate (first-login enrollment wall).
    mfa: Literal["none", "verify", "enroll"]
    session_token: str | None = None
    mfa_token: str | None = None
    provisioning_uri: str | None = None


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str


class SessionResponse(BaseModel):
    session_token: str


class KeepaliveResponse(BaseModel):
    expires_in_seconds: int


class ActiveElevation(BaseModel):
    """A platform operator's current break-glass grant (the tenant they're operating
    in and when it lapses). Null for tenant users and for un-elevated operators."""

    target_tenant_id: UUID
    expires_at: datetime


class MeResponse(BaseModel):
    """The caller's own hydrated session — identity, tenant, display name, role
    names, and effective permissions. Non-PHI (own account metadata)."""

    user_id: UUID
    email: str
    name: str
    account_type: str
    tenant_id: UUID | None
    tenant_slug: str | None
    roles: list[str]
    permissions: list[str]
    active_elevation: ActiveElevation | None = None
    # Login-session timeout config so the client stops hardcoding (and drifting from) these.
    # `login_idle_timeout_seconds` is the config idle window the client counts down from each
    # activity event — the server can't observe mouse/keyboard, so it sends the duration.
    # `login_absolute_remaining_seconds` is the seconds left until the fixed absolute cap; the
    # client turns it into a deadline at receipt (`Date.now() + remaining*1000`), which is
    # skew-safe — mirrors `KeepaliveResponse.expires_in_seconds` rather than shipping an
    # absolute timestamp that client/server clock skew would mis-time the warning against.
    login_idle_timeout_seconds: int
    login_absolute_remaining_seconds: int


class EnrollResponse(BaseModel):
    provisioning_uri: str


class MfaActivateRequest(BaseModel):
    code: str


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


class MfaEnrollActivateRequest(BaseModel):
    mfa_token: str
    code: str


class MfaEnrollActivateResponse(BaseModel):
    session_token: str
    recovery_codes: list[str]


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


class AcceptInviteResponse(BaseModel):
    mfa_required: bool
    provisioning_uri: str | None = None
    mfa_token: str | None = None


class ActivateInviteMfaRequest(BaseModel):
    mfa_token: str
    code: str


class InviteValidateResponse(BaseModel):
    state: Literal["valid", "invalid", "deactivated"]


# --- helpers -----------------------------------------------------------------


def _unauthorized() -> UnauthorizedError:
    # Uniform 401 — no user / provider enumeration.
    return UnauthorizedError(message="invalid credentials")


async def _audit(
    sink: AuthAuditSink,
    *,
    tenant_id: UUID,
    event: AuthEvent,
    ip: str | None,
    user_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    await emit_auth_event(
        sink,
        tenant_id=tenant_id,
        event=event,
        ip=ip,
        user_id=user_id,
        meta={"reason": reason} if reason else {},
    )


@dataclass(frozen=True)
class _PasswordCreds:
    user_id: UUID
    email: str
    hashed_password: str | None
    mfa_enabled: bool
    account_type: str
    status: str


DEACTIVATED_MESSAGE = "Your account has been deactivated. Please contact your administrator."


def raise_for_inactive(creds: _PasswordCreds) -> None:
    """403 for a deactivated account, uniform 401 for any other non-active status.

    MUST be called only AFTER the password has verified: the caller proved
    credential ownership, so naming the reason discloses nothing to outsiders.
    Wrong passwords must keep the uniform 401 (no account-status enumeration).
    """
    if creds.status == "active":
        return
    if creds.status == "deactivated":
        raise CustomAPIException(DefaultExceptionCode.FORBIDDEN, message=DEACTIVATED_MESSAGE)
    raise _unauthorized()


async def _load_password_creds(
    session: AsyncSession, email: str, *, account_type: str | None = None
) -> _PasswordCreds | None:
    """Resolve a user's password credentials within the current session.
    Returns plain values (no lazy ORM attributes) so they survive the session.
    Includes `status` unfiltered — callers gate on it AFTER verifying the
    password (see `raise_for_inactive`).
    `account_type` pins the plane (e.g. 'platform') so a stray row from the other
    plane can never authenticate here; left unset for tenant login (RLS already
    confines the session to one tenant)."""
    row = (
        await session.execute(
            select(
                AppUser.id,
                AppUser.email,
                AppUser.account_type,
                AppUser.status,
                UserIdentity.hashed_password,
                UserIdentity.mfa_enabled,
            )
            .join(AppUser, AppUser.id == UserIdentity.app_user_id)
            .where(
                UserIdentity.provider_type == ProviderKind.PASSWORD.value,
                UserIdentity.email == email,
                *([AppUser.account_type == account_type] if account_type is not None else []),
            )
        )
    ).first()
    if row is None:
        return None
    return _PasswordCreds(
        user_id=row.id,
        email=row.email,
        hashed_password=row.hashed_password,
        mfa_enabled=row.mfa_enabled,
        account_type=row.account_type,
        status=row.status,
    )


async def _stamp_last_login(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: UUID, user_id: UUID
) -> None:
    """Record the moment a login fully succeeds. `now()` is the DB clock — the
    single NTP-synced source (repo CLAUDE.md), never the app clock."""
    async with tenant_session(sessionmaker, tenant_id) as session:
        await session.execute(
            update(AppUser).where(AppUser.id == user_id).values(last_login_at=func.now())
        )


# --- endpoints ---------------------------------------------------------------


@router.post(
    "/tenants/{tenant_slug}/auth/login",
    response_model=ResponseModel[LoginResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def login(
    tenant_slug: str,
    body: LoginRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    kms: KMS,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[LoginResponse]:
    ip = client_ip(request)
    # Unknown/malformed slug → uniform un-audited 401 (no tenant to scope an audit row
    # to), the same posture as the no-provider/unknown-tenant path below.
    tenant_id = await resolve_tenant_id(sessionmaker, tenant_slug)
    if tenant_id is None:
        raise _unauthorized()
    if len(body.password.encode()) > MAX_PASSWORD_BYTES:
        await _audit(audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_FAILURE, ip=ip)
        raise _unauthorized()

    async with tenant_session(sessionmaker, tenant_id) as session:
        provider = await resolve_login_provider(session, tenant_id, ProviderKind.PASSWORD.value)
        # Pin the tenant plane: a platform operator (account_type='platform') must
        # use /platform/auth/login, never this tenant route. In prod RLS already
        # hides the NULL-tenant platform row from this tenant session, but a local
        # superuser DB bypasses RLS — without this pin the platform row would
        # authenticate here and mint a malformed platform+tenant session that every
        # tenant route then rejects with 401. Defense in depth, correct everywhere.
        creds = (
            await _load_password_creds(session, body.email, account_type="tenant")
            if provider is not None
            else None
        )

    if provider is None:
        # No enabled provider resolved — which also covers an unknown tenant. We
        # can't write a tenant-scoped auth_audit_log row (the tenant may not
        # exist; that FK would fail), so this 401 is intentionally un-audited.
        # Every failure past this point has a confirmed-real tenant + provider.
        raise _unauthorized()
    # Constant-time: always run one bcrypt verify, even for an unknown email or a
    # user with no password hash (dummy verify → False). Every failure branch below
    # costs the same, so response time can't reveal whether the email is registered.
    password_ok = verify_password_or_dummy(
        body.password, creds.hashed_password if creds is not None else None
    )
    if creds is None or not password_ok:
        user_id = creds.user_id if creds is not None else None
        await _audit(
            audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=user_id
        )
        raise _unauthorized()
    if creds.status != "active":
        await _audit(
            audit,
            tenant_id=tenant_id,
            event=AuthEvent.LOGIN_FAILURE,
            ip=ip,
            user_id=creds.user_id,
            reason=f"account_{creds.status}",
        )
        raise_for_inactive(creds)

    base = SessionData(
        user_id=creds.user_id,
        tenant_id=tenant_id,
        email=creds.email,
        subject=creds.email,
        provider_type=ProviderKind.PASSWORD.value,
        mfa_passed=False,
        account_type=creds.account_type,
        # Capture the normalized slug for display / invite-URL use; tenant_context
        # derives the tenant id from the session, not the slug. Carried through the
        # MFA challenge via replace(...) below.
        tenant_slug=normalize_slug(tenant_slug),
    )
    if creds.mfa_enabled:
        challenge = await store.put(MFA_NS, base, settings.mfa_challenge_ttl_seconds)
        await _audit(
            audit, tenant_id=tenant_id, event=AuthEvent.MFA_CHALLENGE, ip=ip, user_id=creds.user_id
        )
        return ok(LoginResponse(mfa="verify", mfa_token=challenge))

    if provider.enforce_mfa:
        # Enforced but not yet enrolled — the first-login enrollment wall. Mint a fresh
        # TOTP seed onto the row now and hand back the provisioning URI + a bootstrap
        # token; the user confirms a live code at /auth/mfa/enroll-activate. No session
        # is issued until that code is confirmed.
        async with tenant_session(sessionmaker, tenant_id) as session:
            ident = await _password_identity_row(session, creds.user_id)
            if ident is None:
                raise _unauthorized()
            provisioning_uri = await mfa.enroll(kms, identity=ident, account_email=creds.email)
        enrollment = await store.put(MFA_ENROLL_NS, base, settings.mfa_challenge_ttl_seconds)
        await _audit(
            audit, tenant_id=tenant_id, event=AuthEvent.MFA_CHALLENGE, ip=ip, user_id=creds.user_id
        )
        return ok(
            LoginResponse(mfa="enroll", mfa_token=enrollment, provisioning_uri=provisioning_uri)
        )

    token = await store.mint_session(
        replace(base, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
    await _stamp_last_login(sessionmaker, tenant_id, creds.user_id)
    await _audit(
        audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_SUCCESS, ip=ip, user_id=creds.user_id
    )
    return ok(LoginResponse(mfa="none", session_token=token))


@router.post(
    "/tenants/{tenant_slug}/auth/mfa/verify",
    response_model=ResponseModel[SessionResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def mfa_verify(
    tenant_slug: str,
    body: MfaVerifyRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    kms: KMS,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[SessionResponse]:
    ip = client_ip(request)
    tenant_id = await resolve_tenant_id(sessionmaker, tenant_slug)
    challenge = await store.get(MFA_NS, body.mfa_token)
    if tenant_id is None or challenge is None or challenge.tenant_id != tenant_id:
        raise _unauthorized()

    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, challenge.user_id, for_update=True)
        mfa_result = await mfa.verify(kms, identity=ident, code=body.code) if ident else None
        # Persist the matched TOTP timestep so the code is single-use (replay guard).
        # Tenant identities carry a real tenant_id, so the ORM UPDATE passes RLS.
        # Recovery-code logins (result < 0) have no timestep to record.
        if ident is not None and mfa_result is not None and mfa_result >= 0:
            ident.totp_last_used_timestep = mfa_result
        verified = mfa_result is not None

    if not verified:
        await _audit(
            audit,
            tenant_id=tenant_id,
            event=AuthEvent.LOGIN_FAILURE,
            ip=ip,
            user_id=challenge.user_id,
        )
        raise _unauthorized()

    await store.delete(MFA_NS, body.mfa_token)
    token = await store.mint_session(
        replace(challenge, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
    await _stamp_last_login(sessionmaker, tenant_id, challenge.user_id)
    await _audit(
        audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_SUCCESS, ip=ip, user_id=challenge.user_id
    )
    return ok(SessionResponse(session_token=token))


@router.post(
    "/tenants/{tenant_slug}/auth/mfa/enroll-activate",
    response_model=ResponseModel[MfaEnrollActivateResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def mfa_enroll_activate(
    tenant_slug: str,
    body: MfaEnrollActivateRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    kms: KMS,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[MfaEnrollActivateResponse]:
    """Completes the first-login MFA enrollment wall: confirms a live TOTP code against
    the seed minted at login, then mints the session and returns recovery codes once.
    Unauthenticated, gated by the bootstrap `mfa_token` returned by login."""
    ip = client_ip(request)
    tenant_id = await resolve_tenant_id(sessionmaker, tenant_slug)
    base = await store.get(MFA_ENROLL_NS, body.mfa_token)
    if tenant_id is None or base is None or base.tenant_id != tenant_id:
        raise _unauthorized()

    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, base.user_id)
        if ident is None:
            raise _unauthorized()
        codes = await mfa.activate(kms, identity=ident, code=body.code)
        if codes is not None:
            ident.mfa_enabled = True

    if codes is None:
        await _audit(
            audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=base.user_id
        )
        raise _unauthorized()

    await store.delete(MFA_ENROLL_NS, body.mfa_token)
    token = await store.mint_session(
        replace(base, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
    await _stamp_last_login(sessionmaker, tenant_id, base.user_id)
    await _audit(
        audit, tenant_id=tenant_id, event=AuthEvent.LOGIN_SUCCESS, ip=ip, user_id=base.user_id
    )
    return ok(MfaEnrollActivateResponse(session_token=token, recovery_codes=list(codes)))


@router.post(
    "/auth/logout",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
    ),
)
async def logout(
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    request: Request,
    store: Store,
    audit: AuthAudit,
) -> ResponseModel[None]:
    # Token-scoped self-op: `current_identity` proves a live session (expired → 401),
    # so only real logouts are audited; the slug is irrelevant. delete_session reaps
    # both the `sess` and `sess_abs` keys. A platform operator's tenant_id is None, so
    # emit via emit_auth_event (accepts None → the log_auth_event definer path), not the
    # UUID-only _audit helper.
    if credentials is not None:
        await store.delete_session(credentials.credentials)
    await emit_auth_event(
        audit,
        tenant_id=identity.tenant_id,
        event=AuthEvent.LOGOUT,
        ip=client_ip(request),
        user_id=identity.user_id,
    )
    return ok(None, message="Logged out.")


@router.post(
    "/auth/session/keepalive",
    response_model=ResponseModel[KeepaliveResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
    ),
)
async def keepalive(
    response: Response,
    _identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    store: Store,
    settings: AppSettings,
) -> ResponseModel[KeepaliveResponse]:
    """Slide the caller's own session by the idle window (capped at the absolute max).
    Token-scoped self-op, no tenant context, no PHI, no audit. Returns the new remaining
    seconds so the client can sync its idle timer. `Cache-Control: no-store`."""
    response.headers["Cache-Control"] = "no-store"
    remaining = (
        await store.extend_session(credentials.credentials, settings.session_ttl_seconds)
        if credentials is not None
        else None
    )
    if remaining is None:
        raise UnauthorizedError(message="session expired")
    return ok(KeepaliveResponse(expires_in_seconds=remaining))


@router.get(
    "/auth/me",
    response_model=ResponseModel[MeResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
    ),
)
async def get_me(
    response: Response,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: SelfScopedSession,
    resolver: Resolver,
    store: Store,
    settings: AppSettings,
) -> ResponseModel[MeResponse]:
    """Hydrate the caller's OWN session: identity, tenant, display name, role names,
    and effective permissions. Token-scoped — no tenant in the URL; the RLS scope is
    derived from the verified identity (tenant user pins their tenant, platform operator
    gets the global no-GUC session). No permission gate: any authenticated user may read
    their own session. `Cache-Control: no-store` keeps per-user auth data out of any
    browser/proxy cache."""
    response.headers["Cache-Control"] = "no-store"

    # Permissions reuse the cache-backed resolver; a None id means the account was
    # deactivated/removed since the session was minted → the session is stale.
    resolved_id, permissions = await resolver.effective_permissions(
        session, identity.tenant_id, identity.user_id
    )
    if resolved_id is None:
        raise UnauthorizedError(message="session no longer valid")

    # Seconds left until the fixed absolute cap (`sess_abs` TTL). `current_identity`
    # already proved a live `sess`, which never outlives its companion, so None here
    # means an inconsistent/reaped session → fail closed.
    absolute_remaining = (
        await store.absolute_remaining(credentials.credentials) if credentials is not None else None
    )
    if absolute_remaining is None:
        raise UnauthorizedError(message="session no longer valid")

    # effective_permissions already confirmed the active user exists, so exactly one row
    # resolves here. account_type comes from the verified identity (captured at login,
    # no extra DB column needed).
    row = (await session.execute(select(AppUser.name).where(AppUser.id == identity.user_id))).one()

    # RLS confines user_role to this tenant and exposes the global (NULL-tenant) system
    # roles via the catalog policy, so a plain join resolves both — no manual tenant clause.
    roles = (
        (
            await session.execute(
                select(Role.name)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.app_user_id == identity.user_id)
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    # Platform operators carry their active break-glass grant so the UI can reveal
    # tenant-scoped surfaces only while elevated. Tenant users never have one.
    active_elevation = None
    if identity.account_type is AccountType.PLATFORM:
        grant = await elevation.active_grant_for_operator(session, operator=identity.user_id)
        if grant is not None:
            active_elevation = ActiveElevation(
                target_tenant_id=grant.target_tenant_id, expires_at=grant.expires_at
            )

    return ok(
        MeResponse(
            user_id=identity.user_id,
            email=identity.email,
            name=row.name,
            account_type=identity.account_type.value,
            tenant_id=identity.tenant_id,
            tenant_slug=identity.tenant_slug,
            roles=sorted(roles),
            permissions=sorted(permissions),
            active_elevation=active_elevation,
            login_idle_timeout_seconds=settings.session_ttl_seconds,
            login_absolute_remaining_seconds=absolute_remaining,
        )
    )


@router.post(
    "/auth/mfa/enroll",
    response_model=ResponseModel[EnrollResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def mfa_enroll(
    tenant_id: TenantId,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Sessionmaker,
    kms: KMS,
) -> ResponseModel[EnrollResponse]:
    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, identity.user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        uri = await mfa.enroll(kms, identity=ident, account_email=identity.email)
    return ok(EnrollResponse(provisioning_uri=uri))


@router.post(
    "/auth/mfa/activate",
    response_model=ResponseModel[RecoveryCodesResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def mfa_activate(
    tenant_id: TenantId,
    body: MfaActivateRequest,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Sessionmaker,
    kms: KMS,
) -> ResponseModel[RecoveryCodesResponse]:
    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, identity.user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        codes = await mfa.activate(kms, identity=ident, code=body.code)
        if codes is None:
            raise BadRequestError(message="invalid code")
        ident.mfa_enabled = True
    return ok(RecoveryCodesResponse(recovery_codes=list(codes)))


async def _password_identity_row(
    session: AsyncSession, user_id: UUID, *, for_update: bool = False
) -> UserIdentity | None:
    q = select(UserIdentity).where(
        UserIdentity.app_user_id == user_id,
        UserIdentity.provider_type == ProviderKind.PASSWORD.value,
    )
    if for_update:
        q = q.with_for_update()
    return (await session.execute(q)).scalar_one_or_none()


@router.get(
    "/tenants/{tenant_slug}/auth/invitations/validate",
    response_model=ResponseModel[InviteValidateResponse],
)
async def validate_invitation(
    tenant_slug: str,
    token: str,
    response: Response,
    sessionmaker: Sessionmaker,
    invites: Invites,
) -> ResponseModel[InviteValidateResponse]:
    """Token-scoped invite pre-flight: returns the eligibility state without
    consuming the token or revealing any PHI. Because the caller must already
    possess the high-entropy secret token, this does not enable enumeration.
    `Cache-Control: no-store` — the result reflects live DB state."""
    response.headers["Cache-Control"] = "no-store"
    tenant_id, invite = await asyncio.gather(
        resolve_tenant_id(sessionmaker, tenant_slug),
        invites.get(INVITE_NS, token),
    )
    if tenant_id is None or invite is None or invite.tenant_id != tenant_id:
        return ok(InviteValidateResponse(state="invalid"))

    async with tenant_session(sessionmaker, tenant_id) as session:
        row = (
            await session.execute(select(AppUser.status).where(AppUser.id == invite.app_user_id))
        ).one_or_none()

    if row is None:
        return ok(InviteValidateResponse(state="invalid"))
    if row.status == "invited":
        return ok(InviteValidateResponse(state="valid"))
    if row.status == "deactivated":
        return ok(InviteValidateResponse(state="deactivated"))
    # already activated, or any other non-invited, non-deactivated status → invalid
    return ok(InviteValidateResponse(state="invalid"))


@router.post(
    "/tenants/{tenant_slug}/auth/invitations/accept",
    response_model=ResponseModel[AcceptInviteResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def accept_invitation(
    tenant_slug: str,
    body: AcceptInviteRequest,
    request: Request,
    response: Response,
    sessionmaker: Sessionmaker,
    kms: KMS,
    audit: AuthAudit,
    invites: Invites,
    settings: AppSettings,
) -> ResponseModel[AcceptInviteResponse]:
    """Unauthenticated, token-gated: an invited user sets their password. If the
    tenant enforces MFA, this returns a provisioning URI + a bridge `mfa_token` and
    leaves the account `invited` until `activate-mfa`; otherwise the account goes
    `active`. The invite token is single-use (consumed here)."""
    response.headers["Cache-Control"] = "no-store"
    tenant_id = await resolve_tenant_id(sessionmaker, tenant_slug)
    invite = await invites.get(INVITE_NS, body.token)
    if tenant_id is None or invite is None or invite.tenant_id != tenant_id:
        raise _unauthorized()
    if len(body.password.encode()) > MAX_PASSWORD_BYTES:
        raise BadRequestError(message="password too long")

    provisioning_uri: str | None = None
    enforce_mfa = False
    async with tenant_session(sessionmaker, tenant_id) as session:
        user = (
            await session.execute(select(AppUser).where(AppUser.id == invite.app_user_id))
        ).scalar_one_or_none()
        if user is None or user.status != "invited":
            # Already accepted / deactivated / gone — the single-use invite is spent.
            raise _unauthorized()
        if await _password_identity_row(session, user.id) is not None:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT, message="invitation already accepted"
            )
        provider_row = (
            await session.execute(
                select(SsoProvider).where(SsoProvider.provider_type == ProviderKind.PASSWORD.value)
            )
        ).scalar_one_or_none()
        enforce_mfa = provider_row.enforce_mfa if provider_row is not None else False

        identity = UserIdentity(
            tenant_id=tenant_id,
            app_user_id=user.id,
            provider_type=ProviderKind.PASSWORD.value,
            provider_subject=invite.email,
            email=invite.email,
            hashed_password=hash_password(body.password),
            mfa_enabled=False,
        )
        session.add(identity)
        if enforce_mfa:
            provisioning_uri = await mfa.enroll(kms, identity=identity, account_email=invite.email)
        else:
            user.status = "active"

    await invites.delete(INVITE_NS, body.token)
    await _audit(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.INVITE_ACCEPTED,
        ip=client_ip(request),
        user_id=invite.app_user_id,
    )
    if enforce_mfa:
        mfa_token = await invites.put(INVITE_MFA_NS, invite, settings.invite_ttl_seconds)
        return ok(
            AcceptInviteResponse(
                mfa_required=True, provisioning_uri=provisioning_uri, mfa_token=mfa_token
            )
        )
    return ok(AcceptInviteResponse(mfa_required=False))


@router.post(
    "/tenants/{tenant_slug}/auth/invitations/activate-mfa",
    response_model=ResponseModel[RecoveryCodesResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def activate_invitation_mfa(
    tenant_slug: str,
    body: ActivateInviteMfaRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    kms: KMS,
    audit: AuthAudit,
    invites: Invites,
) -> ResponseModel[RecoveryCodesResponse]:
    """Unauthenticated, token-gated: completes MFA enrollment for an enforce_mfa
    tenant, flipping the account to `active` and returning recovery codes once."""
    tenant_id = await resolve_tenant_id(sessionmaker, tenant_slug)
    invite = await invites.get(INVITE_MFA_NS, body.mfa_token)
    if tenant_id is None or invite is None or invite.tenant_id != tenant_id:
        raise _unauthorized()

    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, invite.app_user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        codes = await mfa.activate(kms, identity=ident, code=body.code)
        if codes is None:
            raise BadRequestError(message="invalid code")
        ident.mfa_enabled = True
        await session.execute(
            update(AppUser).where(AppUser.id == invite.app_user_id).values(status="active")
        )
    await invites.delete(INVITE_MFA_NS, body.mfa_token)
    await _audit(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.INVITE_ACCEPTED,
        ip=client_ip(request),
        user_id=invite.app_user_id,
    )
    return ok(RecoveryCodesResponse(recovery_codes=list(codes)))
