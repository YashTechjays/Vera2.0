"""Identity-provider management (spec §4.1.3) — a TENANT_ADMIN enables or disables
the tenant's login providers and sets MFA enforcement.

Gated by `tenant:auth:configure` and audited. Enabling the local `password`
provider requires `enforce_mfa=True` (spec rule). Wiring a federated provider's
GCIP config (gcip_provider_id) is a separate SUPER_ADMIN platform operation
(Phase 1) — this endpoint only flips the tenant-owned `enabled`/`enforce_mfa`
toggles on the `sso_provider` row.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from control_plane.api.v1.common import AuthAudit, TenantId, TenantSession, emit_auth_event
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import client_ip
from control_plane.exceptions import BadRequestError, CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.models import SsoProvider
from vera_core.models.enums import AuthEvent, ProviderKind, values_of

router = APIRouter(tags=["providers"])


class ProviderResponse(BaseModel):
    provider_type: str
    display_name: str
    enabled: bool
    enforce_mfa: bool


class UpdateProviderRequest(BaseModel):
    enabled: bool
    enforce_mfa: bool


def _to_response(row: SsoProvider) -> ProviderResponse:
    return ProviderResponse(
        provider_type=row.provider_type,
        display_name=row.display_name,
        enabled=row.enabled,
        enforce_mfa=row.enforce_mfa,
    )


@router.get(
    "/auth/providers",
    response_model=ResponseModel[list[ProviderResponse]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_providers(
    _tenant_id: TenantId,
    session: TenantSession,
    _caller: VerifiedIdentity = require("tenant:auth:configure"),
) -> ResponseModel[list[ProviderResponse]]:
    rows = (
        (await session.execute(select(SsoProvider).order_by(SsoProvider.provider_type)))
        .scalars()
        .all()
    )
    return ok([_to_response(r) for r in rows])


@router.patch(
    "/auth/providers/{provider_type}",
    response_model=ResponseModel[ProviderResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def update_provider(
    provider_type: str,
    body: UpdateProviderRequest,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    audit: AuthAudit,
    _caller: VerifiedIdentity = require("tenant:auth:configure"),
) -> ResponseModel[ProviderResponse]:
    if provider_type not in values_of(ProviderKind):
        raise BadRequestError(message="unknown provider type")
    # Spec rule: the local password provider may only be enabled when MFA is enforced.
    if provider_type == ProviderKind.PASSWORD.value and body.enabled and not body.enforce_mfa:
        raise BadRequestError(message="password provider requires MFA enforcement")

    row = (
        await session.execute(select(SsoProvider).where(SsoProvider.provider_type == provider_type))
    ).scalar_one_or_none()
    if row is None:
        row = SsoProvider(
            tenant_id=tenant_id,
            provider_type=provider_type,
            display_name=provider_type,
        )
        session.add(row)
    row.enabled = body.enabled
    row.enforce_mfa = body.enforce_mfa

    await emit_auth_event(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.PROVIDER_ENABLED if body.enabled else AuthEvent.PROVIDER_DISABLED,
        ip=client_ip(request),
        user_id=_caller.user_id,
        meta={"provider_type": provider_type, "enforce_mfa": body.enforce_mfa},
    )
    return ok(_to_response(row))
