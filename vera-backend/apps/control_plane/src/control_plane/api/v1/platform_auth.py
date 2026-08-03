"""Platform-operator login (ADR-0006 §D, interim password+MFA).

Tenant-less sibling of `auth.py`'s tenant login: no `{tenant_slug}` — the operator
belongs to no tenant. Credentials + provider config resolve inside a `platform_session`
(`app.platform='on'`, no tenant GUC), which is exactly the RLS context that exposes the
NULL-tenant `app_user` / `user_identity` / `platform_login_provider` rows and nothing else
(zero PHI). MFA is mandatory: an enrolled operator gets a `verify` challenge completed at
`/mfa/verify`; a not-yet-enrolled operator hits the first-login enrollment wall — login
issues the seed + QR only while inside the time-boxed enrollment window after bootstrap
(`platform_enroll_window_seconds`), so a leaked password can't bind a second factor once
the window closes. Enrollment completes at `/mfa/enroll-activate`. Either path
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
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.auth import (
    AcceptInviteRequest,
    AcceptInviteResponse,
    ActivateInviteMfaRequest,
    InviteValidateResponse,
    LoginRequest,
    LoginResponse,
    MfaEnrollActivateRequest,
    MfaVerifyRequest,
    SessionResponse,
    _load_password_creds,
    _password_identity_row,
    _unauthorized,
    raise_for_inactive,
)
from control_plane.api.v1.common import AppSettings, AuthAudit, Invites
from control_plane.auth import mfa
from control_plane.auth.invitations import PLATFORM_INVITE_MFA_NS, PLATFORM_INVITE_NS
from control_plane.auth.password import MAX_PASSWORD_BYTES, hash_password, verify_password_or_dummy
from control_plane.auth.platform_provisioning import create_password_identity, set_operator_status
from control_plane.auth.providers import resolve_platform_login_provider
from control_plane.auth.session import MFA_ENROLL_NS, MFA_NS, SessionData, SessionStore
from control_plane.deps import client_ip, get_kms, get_session_store, get_sessionmaker
from control_plane.exceptions import (
    BadRequestError,
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
)
from control_plane.responses import ResponseModel, ok
from vera_core.audit import emit_auth_event
from vera_core.config.kms import KeyManagementService
from vera_core.db import platform_session
from vera_core.models import AppUser
from vera_core.models.enums import AccountType, AuthEvent, ProviderKind

router = APIRouter(prefix="/platform/auth", tags=["platform-auth"])


Sessionmaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
Store = Annotated[SessionStore, Depends(get_session_store)]
KMS = Annotated[KeyManagementService, Depends(get_kms)]


async def _enroll_window_open(
    session: AsyncSession, identity_id: UUID, window_seconds: int
) -> bool:
    """True while the operator is still inside their first-login enrollment window,
    measured on the DB clock from the identity's creation — a leaked bootstrap password
    can't bind a second factor once the window closes (ADR-0006 §D)."""
    still_open = (
        await session.execute(
            text(
                "SELECT created_at + make_interval(secs => :secs) > now() "
                "FROM user_identity WHERE id = :id"
            ).bindparams(secs=window_seconds, id=identity_id)
        )
    ).scalar_one()
    return bool(still_open)


def _require_platform_challenge(data: SessionData | None) -> SessionData:
    """Fail closed unless the challenge is platform-plane — account_type AND tenant_id
    IS NULL (both round-trip through Redis and can drift; CLAUDE.md)."""
    if (
        data is None
        or data.account_type != AccountType.PLATFORM.value
        or data.tenant_id is not None
    ):
        raise _unauthorized()
    return data


@router.get(
    "/invitations/validate",
    response_model=ResponseModel[InviteValidateResponse],
)
async def validate_platform_invitation(
    token: str,
    response: Response,
    sessionmaker: Sessionmaker,
    invites: Invites,
) -> ResponseModel[InviteValidateResponse]:
    """Token-scoped invite pre-flight for a platform operator — no tenant slug, the
    invitee belongs to no tenant. Mirrors validate_invitation (auth.py)."""
    response.headers["Cache-Control"] = "no-store"
    invite = await invites.get(PLATFORM_INVITE_NS, token)
    if invite is None or invite.tenant_id is not None:
        return ok(InviteValidateResponse(state="invalid"))

    async with platform_session(sessionmaker) as session:
        row = (
            await session.execute(select(AppUser.status).where(AppUser.id == invite.app_user_id))
        ).one_or_none()

    if row is None:
        return ok(InviteValidateResponse(state="invalid"))
    if row.status == "invited":
        return ok(InviteValidateResponse(state="valid"))
    if row.status == "deactivated":
        return ok(InviteValidateResponse(state="deactivated"))
    return ok(InviteValidateResponse(state="invalid"))


@router.post(
    "/invitations/accept",
    response_model=ResponseModel[AcceptInviteResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def accept_platform_invitation(
    body: AcceptInviteRequest,
    request: Request,
    response: Response,
    sessionmaker: Sessionmaker,
    kms: KMS,
    audit: AuthAudit,
    invites: Invites,
    settings: AppSettings,
) -> ResponseModel[AcceptInviteResponse]:
    """Unauthenticated, token-gated: a platform invitee sets their password. MFA is
    ALWAYS required for platform operators — no enforce_mfa branch, unlike the
    tenant flow — this always returns a provisioning URI + bridge mfa_token and
    leaves status "invited" until activate-mfa. Single-use (token consumed here)."""
    response.headers["Cache-Control"] = "no-store"
    invite = await invites.get(PLATFORM_INVITE_NS, body.token)
    if invite is None or invite.tenant_id is not None:
        raise _unauthorized()
    if len(body.password.encode()) > MAX_PASSWORD_BYTES:
        raise BadRequestError(message="password too long")

    async with platform_session(sessionmaker) as session:
        user = (
            await session.execute(select(AppUser).where(AppUser.id == invite.app_user_id))
        ).scalar_one_or_none()
        if user is None or user.status != "invited":
            raise _unauthorized()
        if await _password_identity_row(session, user.id) is not None:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT, message="invitation already accepted"
            )
        await create_password_identity(
            session,
            app_user_id=user.id,
            email=invite.email,
            hashed_password=hash_password(body.password),
        )
        identity = await _password_identity_row(session, user.id)
        assert identity is not None  # just created above, in the same transaction
        provisioning_uri = await mfa.enroll_platform(
            kms, session, identity=identity, account_email=invite.email
        )

    await invites.delete(PLATFORM_INVITE_NS, body.token)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_INVITE_ACCEPTED,
        ip=client_ip(request),
        user_id=invite.app_user_id,
    )
    mfa_token = await invites.put(PLATFORM_INVITE_MFA_NS, invite, settings.invite_ttl_seconds)
    return ok(
        AcceptInviteResponse(
            mfa_required=True, provisioning_uri=provisioning_uri, mfa_token=mfa_token
        )
    )


@router.post(
    "/invitations/activate-mfa",
    response_model=ResponseModel[None],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.CONFLICT,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def activate_platform_invitation_mfa(
    body: ActivateInviteMfaRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    kms: KMS,
    audit: AuthAudit,
    invites: Invites,
) -> ResponseModel[None]:
    """Completes MFA enrollment for a platform invitee, flipping status to active.
    No recovery codes are returned — platform MFA is TOTP-only everywhere in this
    codebase (see mfa.py module docstring); consuming a recovery code would need
    yet another definer write on an already-enrolled row."""
    invite = await invites.get(PLATFORM_INVITE_MFA_NS, body.mfa_token)
    if invite is None or invite.tenant_id is not None:
        raise _unauthorized()

    async with platform_session(sessionmaker) as session:
        ident = await _password_identity_row(session, invite.app_user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        activated = await mfa.activate_platform(kms, session, identity=ident, code=body.code)
        if not activated:
            raise BadRequestError(message="invalid code")
        flipped = await set_operator_status(
            session, app_user_id=invite.app_user_id, status="active"
        )
        if not flipped:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT, message="could not activate operator"
            )

    await invites.delete(PLATFORM_INVITE_MFA_NS, body.mfa_token)
    await emit_auth_event(
        audit,
        tenant_id=None,
        event=AuthEvent.PLATFORM_USER_ACTIVATED,
        ip=client_ip(request),
        user_id=invite.app_user_id,
    )
    return ok(None, message="Platform operator activated.")


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
        # First-login wall, time-boxed: only issue a QR while the operator is still inside
        # the enrollment window (from bootstrap), so a leaked password can't enroll later.
        async with platform_session(sessionmaker) as session:
            ident = await _password_identity_row(session, creds.user_id)
            if ident is None:
                raise _unauthorized()
            window_open = await _enroll_window_open(
                session, ident.id, settings.platform_enroll_window_seconds
            )
            provisioning_uri = (
                await mfa.enroll_platform(kms, session, identity=ident, account_email=creds.email)
                if window_open
                else None
            )
        if not window_open:
            await emit_auth_event(
                audit, tenant_id=None, event=AuthEvent.LOGIN_FAILURE, ip=ip, user_id=creds.user_id
            )
            raise _unauthorized()
        if provisioning_uri is None:  # already enrolled between check and write — retry as verify
            challenge = await store.mint_mfa_challenge(
                MFA_NS, base, settings.mfa_challenge_ttl_seconds
            )
            await emit_auth_event(
                audit, tenant_id=None, event=AuthEvent.MFA_CHALLENGE, ip=ip, user_id=creds.user_id
            )
            return ok(LoginResponse(mfa="verify", mfa_token=challenge))
        enrollment = await store.mint_mfa_challenge(
            MFA_ENROLL_NS, base, settings.mfa_challenge_ttl_seconds
        )
        await emit_auth_event(
            audit, tenant_id=None, event=AuthEvent.MFA_CHALLENGE, ip=ip, user_id=creds.user_id
        )
        return ok(
            LoginResponse(mfa="enroll", mfa_token=enrollment, provisioning_uri=provisioning_uri)
        )

    challenge = await store.mint_mfa_challenge(MFA_NS, base, settings.mfa_challenge_ttl_seconds)
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
    challenge = _require_platform_challenge(await store.get(MFA_NS, body.mfa_token))

    result: int | None = None
    async with platform_session(sessionmaker) as session:
        # FOR UPDATE serializes concurrent same-window verifications so a replayed
        # TOTP code can't slip through the read-check-write race (mirrors the tenant
        # path); the definer's monotonic guard is the second line of defence.
        ident = await _password_identity_row(session, challenge.user_id, for_update=True)
        if ident is not None:
            result = await mfa.verify(kms, identity=ident, code=body.code)
            # Persist the matched TOTP timestep for platform (NULL-tenant) identities via
            # SECURITY DEFINER — the RLS-bound role cannot UPDATE NULL-tenant rows directly.
            # Recovery-code logins (result < 0) don't have a timestep to record.
            if result is not None and result >= 0:
                await mfa.platform_update_totp_last_used(session, identity=ident, step=result)

    if result is None:
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
    response_model=ResponseModel[SessionResponse],
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
) -> ResponseModel[SessionResponse]:
    """Complete the platform first-login enrollment wall: confirm a live TOTP code
    against the seed minted at login, flip mfa_enabled via the definer path, and mint
    the session. TOTP-only — no recovery codes (see module docstring). Gated by the
    enrollment `mfa_token`."""
    ip = client_ip(request)
    base = _require_platform_challenge(await store.get(MFA_ENROLL_NS, body.mfa_token))

    activated = False
    async with platform_session(sessionmaker) as session:
        ident = await _password_identity_row(session, base.user_id)
        if ident is not None:
            activated = await mfa.activate_platform(kms, session, identity=ident, code=body.code)

    if not activated:
        # Burn the challenge on a failed code — no attempt cap, so a live token would be
        # a full-TTL brute-force window against the second factor.
        await store.delete(MFA_ENROLL_NS, body.mfa_token)
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
    return ok(SessionResponse(session_token=token))
