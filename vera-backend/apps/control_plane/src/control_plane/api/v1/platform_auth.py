"""Platform-operator login (ADR-0006 §D, interim password+MFA).

Tenant-less sibling of `auth.py`'s tenant login: no `{tenant_slug}` — the operator
belongs to no tenant. Credentials + provider config resolve inside a `platform_session`
(`app.platform='on'`, no tenant GUC), which is exactly the RLS context that exposes the
NULL-tenant `app_user` / `user_identity` / `platform_login_provider` rows and nothing else
(zero PHI). MFA is mandatory: an enrolled operator gets a `verify` challenge completed at
`/mfa/verify`; a not-yet-enrolled operator hits the first-login enrollment wall — login mints
the seed and returns an `enroll` challenge completed at `/mfa/enroll-activate`. Either path
mints the `account_type='platform'`, `tenant_id=None` session. Failures
return a uniform 401 (no operator/provider enumeration); outcomes are audited to
auth_audit_log with `tenant_id=NULL`.

No `last_login_at` stamping: the platform-readable policy's WITH CHECK is strict equality, so
the RLS-bound app role cannot UPDATE a NULL-tenant `app_user` row. Login time is recorded via
the `LOGIN_SUCCESS` auth-audit row instead (adr/devops-todo.md). For the same reason TOTP is
the only supported second factor here — recovery-code consumption would mutate the NULL-tenant
row and fail the strict WITH CHECK; `mfa.verify` accepts a current TOTP as a pure read.
"""

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.auth import (
    LoginRequest,
    LoginResponse,
    MfaEnrollActivateRequest,
    MfaEnrollActivateResponse,
    MfaVerifyRequest,
    SessionResponse,
    _load_password_creds,
    _password_identity_row,
    _unauthorized,
    raise_for_inactive,
)
from control_plane.api.v1.common import AppSettings, AuthAudit, emit_auth_event
from control_plane.auth import mfa
from control_plane.auth.password import MAX_PASSWORD_BYTES, verify_password_or_dummy
from control_plane.auth.providers import resolve_platform_login_provider
from control_plane.auth.session import MFA_ENROLL_NS, MFA_NS, SessionData, SessionStore
from control_plane.deps import client_ip, get_kms, get_session_store, get_sessionmaker
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.config.kms import KeyManagementService
from vera_core.db import platform_session
from vera_core.models.enums import AccountType, AuthEvent, ProviderKind

router = APIRouter(prefix="/platform/auth", tags=["platform-auth"])

Sessionmaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
Store = Annotated[SessionStore, Depends(get_session_store)]
KMS = Annotated[KeyManagementService, Depends(get_kms)]


@router.post(
    "/login",
    response_model=ResponseModel[LoginResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def platform_login(
    body: LoginRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    audit: AuthAudit,
    settings: AppSettings,
    kms: KMS,
) -> ResponseModel[LoginResponse]:
    ip = client_ip(request)
    if len(body.password.encode()) > MAX_PASSWORD_BYTES:
        await emit_auth_event(audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip)
        raise _unauthorized()

    async with platform_session(sessionmaker) as session:
        provider = await resolve_platform_login_provider(session, ProviderKind.PASSWORD.value)
        # account_type pins the platform plane so a stray tenant row can never authenticate here.
        creds = (
            await _load_password_creds(session, body.email, account_type=AccountType.PLATFORM.value)
            if provider is not None
            else None
        )

    if provider is None:
        # Password login disabled globally — uniform 401, un-audited (no operator to scope to).
        raise _unauthorized()
    # Constant-time: always run one bcrypt verify, even for an unknown email or a
    # user with no password hash (dummy verify → False). Every failure branch below
    # costs the same, so response time can't reveal whether an operator email exists.
    password_ok = verify_password_or_dummy(
        body.password, creds.hashed_password if creds is not None else None
    )
    if creds is None or not password_ok:
        user_id = creds.user_id if creds is not None else None
        await emit_auth_event(
            audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=user_id
        )
        raise _unauthorized()
    if creds.status != "active":
        await emit_auth_event(
            audit,
            tenant_id=None,
            event=AuthEvent.LOGIN_FAILURE,
            ip=ip,
            user_id=creds.user_id,
            meta={"reason": f"account_{creds.status}"},
        )
        raise_for_inactive(creds)

    base = SessionData(
        user_id=creds.user_id,
        tenant_id=None,
        email=creds.email,
        subject=creds.email,
        provider_type=ProviderKind.PASSWORD.value,
        mfa_passed=False,
        account_type=AccountType.PLATFORM.value,
        tenant_slug=None,
    )
    # MFA is mandatory for platform operators: login NEVER mints a session directly.
    if not creds.mfa_enabled:
        # First-login enrollment wall: mint the seed via the definer path and hand back
        # the QR. No session until /mfa/enroll-activate confirms a live code.
        async with platform_session(sessionmaker) as session:
            ident = await _password_identity_row(session, creds.user_id)
            if ident is None:
                raise _unauthorized()
            provisioning_uri = await mfa.enroll_platform(
                kms, session, identity=ident, account_email=creds.email
            )
        if provisioning_uri is None:  # already enrolled between check and write — retry as verify
            challenge = await store.put(MFA_NS, base, settings.mfa_challenge_ttl_seconds)
            return ok(LoginResponse(mfa="verify", mfa_token=challenge))
        enrollment = await store.put(MFA_ENROLL_NS, base, settings.mfa_challenge_ttl_seconds)
        await emit_auth_event(
            audit, tenant_id=None, event=AuthEvent.MFA_CHALLENGE, ip=ip, user_id=creds.user_id
        )
        return ok(
            LoginResponse(mfa="enroll", mfa_token=enrollment, provisioning_uri=provisioning_uri)
        )

    challenge = await store.put(MFA_NS, base, settings.mfa_challenge_ttl_seconds)
    await emit_auth_event(
        audit, tenant_id=None, event=AuthEvent.MFA_CHALLENGE, ip=ip, user_id=creds.user_id
    )
    return ok(LoginResponse(mfa="verify", mfa_token=challenge))


@router.post(
    "/mfa/verify",
    response_model=ResponseModel[SessionResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def platform_mfa_verify(
    body: MfaVerifyRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    kms: KMS,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[SessionResponse]:
    ip = client_ip(request)
    challenge = await store.get(MFA_NS, body.mfa_token)
    if challenge is None or challenge.account_type != AccountType.PLATFORM.value:
        raise _unauthorized()

    verified = False
    async with platform_session(sessionmaker) as session:
        ident = await _password_identity_row(session, challenge.user_id)
        if ident is not None:
            verified = await mfa.verify(kms, identity=ident, code=body.code)

    if not verified:
        await emit_auth_event(
            audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=challenge.user_id
        )
        raise _unauthorized()

    await store.delete(MFA_NS, body.mfa_token)
    token = await store.mint_session(
        replace(challenge, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
    await emit_auth_event(
        audit, tenant_id=None, event=AuthEvent.LOGIN_SUCCESS, ip=ip, user_id=challenge.user_id
    )
    return ok(SessionResponse(session_token=token))


@router.post(
    "/mfa/enroll-activate",
    response_model=ResponseModel[MfaEnrollActivateResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def platform_mfa_enroll_activate(
    body: MfaEnrollActivateRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    kms: KMS,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[MfaEnrollActivateResponse]:
    """Complete the platform first-login enrollment wall: confirm a live TOTP code
    against the seed minted at login, flip mfa_enabled via the definer path, mint the
    session, and return recovery codes once. Gated by the enrollment `mfa_token`."""
    ip = client_ip(request)
    base = await store.get(MFA_ENROLL_NS, body.mfa_token)
    if base is None or base.account_type != AccountType.PLATFORM.value:
        raise _unauthorized()

    codes: tuple[str, ...] | None = None
    async with platform_session(sessionmaker) as session:
        ident = await _password_identity_row(session, base.user_id)
        if ident is not None:
            codes = await mfa.activate_platform(kms, session, identity=ident, code=body.code)

    if codes is None:
        await emit_auth_event(
            audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=base.user_id
        )
        raise _unauthorized()

    await store.delete(MFA_ENROLL_NS, body.mfa_token)
    token = await store.mint_session(
        replace(base, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
    await emit_auth_event(
        audit, tenant_id=None, event=AuthEvent.LOGIN_SUCCESS, ip=ip, user_id=base.user_id
    )
    return ok(MfaEnrollActivateResponse(session_token=token, recovery_codes=list(codes)))
