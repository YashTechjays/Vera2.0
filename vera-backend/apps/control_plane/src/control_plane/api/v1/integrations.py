"""Tenant outbound integration credentials (spec §4.1.2).

A TENANT_ADMIN configures the per-tenant credential for an integration type (e.g.
the Twilio SIP trunk). The credential is **envelope-encrypted in the DB**
(`vera_core.integrations.credentials`); the plaintext is accepted once on write
and never returned. Gated by `integrations:manage`; RLS scopes every row to the
caller's tenant, so a tenant can only ever see/set its own credential.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from control_plane.api.v1.common import LiveKit, TenantId, TenantSession
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.deps import get_audit, get_kms
from control_plane.exceptions import (
    CustomAPIException,
    CustomAPIResponse,
    DefaultExceptionCode,
    NotFoundError,
)
from control_plane.livekit_gateway import LiveKitGateway, LiveKitUnavailable
from control_plane.responses import ResponseModel, ok
from vera_core.audit import AuditRecord
from vera_core.integrations.credentials import seal_credentials
from vera_core.models import Integration, IntegrationType
from vera_core.models.audit_log import ActorType, AuditEvent

router = APIRouter(tags=["integrations"])


class IntegrationSummary(BaseModel):
    """Non-secret view of a tenant's integration — never carries the credential."""

    integration_type: str
    status: str
    configured: bool
    rotated_at: datetime | None


class ConfigureIntegrationRequest(BaseModel):
    # Credential payload matching the type's `credentials_schema`, e.g.
    # {"trunk_id": "TK…"}.
    credentials: dict[str, Any]


def _validate_credentials(schema: dict[str, Any], credentials: dict[str, Any]) -> None:
    """Each schema key must be present as a non-empty string; reject unknown keys.
    Raises VALIDATION_ERROR carrying the offending field paths (never the values)."""
    bad = {
        k for k in schema if not isinstance(credentials.get(k), str) or not credentials[k].strip()
    }
    bad |= {k for k in credentials if k not in schema}
    if bad:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="invalid integration credentials",
            data={"fields": sorted(bad)},
        )


async def _validate_livekit_outbound_trunk(
    credentials: dict[str, Any], *, livekit: LiveKitGateway
) -> None:
    """Probe LiveKit so we never store a trunk id LiveKit doesn't recognise. This is
    an existence check only — LiveKit confirms it holds an outbound trunk with this id,
    NOT that the trunk's upstream provider credentials work (that surfaces only when a
    call is placed). Reject (422) an unknown id; fail closed (502) if LiveKit can't be
    reached, so an unverified trunk is never stored (the configured strict behaviour)."""
    trunk_id = credentials["trunk_id"]  # schema validation guarantees it is present
    try:
        exists = await livekit.outbound_trunk_exists(trunk_id)
    except LiveKitUnavailable as e:
        raise CustomAPIException(
            DefaultExceptionCode.BAD_GATEWAY,
            message="could not verify the trunk id — the LiveKit SIP service is unreachable",
        ) from e
    if not exists:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="trunk id not recognised by the LiveKit SIP service",
            data={"fields": ["trunk_id"]},
        )


# Per-type credential validators that probe the upstream service before we seal a
# credential. A type with no entry is stored after schema validation only (current
# behaviour); registering one is how a new integration adds a test-before-save check.
# Each takes the submitted credentials plus the request-scoped LiveKit gateway (the
# only upstream a validator needs today).
INTEGRATION_VALIDATORS: dict[str, Callable[[dict[str, Any], LiveKitGateway], Awaitable[None]]] = {
    "livekit_outbound_trunk_id": lambda creds, livekit: _validate_livekit_outbound_trunk(
        creds, livekit=livekit
    ),
}


@router.get(
    "/integrations",
    response_model=ResponseModel[list[IntegrationSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def list_integrations(
    session: TenantSession,
    _tenant_id: TenantId,
    _caller: VerifiedIdentity = require("integrations:manage"),
) -> ResponseModel[list[IntegrationSummary]]:
    rows = (
        await session.execute(
            select(Integration, IntegrationType).join(
                IntegrationType, IntegrationType.id == Integration.integration_type_id
            )
        )
    ).all()
    items = [
        IntegrationSummary(
            integration_type=itype.name,
            status=integration.status,
            configured=integration.credential_ct is not None,
            rotated_at=integration.rotated_at,
        )
        for integration, itype in rows
    ]
    return ok(items)


@router.put(
    "/integrations/{integration_type}",
    response_model=ResponseModel[IntegrationSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.BAD_GATEWAY,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def configure_integration(
    integration_type: str,
    body: ConfigureIntegrationRequest,
    request: Request,
    session: TenantSession,
    tenant_id: TenantId,
    livekit: LiveKit,
    caller: VerifiedIdentity = require("integrations:manage"),
) -> ResponseModel[IntegrationSummary]:
    """Create or replace this tenant's credential for `integration_type` (upsert on
    the unique (tenant_id, integration_type_id)). Validate shape, probe the upstream
    service (test-before-save), then envelope-encrypt."""
    itype = (
        await session.execute(
            select(IntegrationType).where(IntegrationType.name == integration_type)
        )
    ).scalar_one_or_none()
    if itype is None:
        raise NotFoundError(message="unknown integration type")

    _validate_credentials(itype.credentials_schema, body.credentials)

    # Type-specific upstream check (e.g. confirm the LiveKit trunk id exists) before we
    # ever seal an unusable credential. No validator registered → schema check only.
    validator = INTEGRATION_VALIDATORS.get(integration_type)
    if validator is not None:
        await validator(body.credentials, livekit)

    integration = (
        await session.execute(
            select(Integration).where(Integration.integration_type_id == itype.id)
        )
    ).scalar_one_or_none()
    if integration is None:
        integration = Integration(tenant_id=tenant_id, integration_type_id=itype.id)
        session.add(integration)

    await seal_credentials(get_kms(request), integration=integration, credentials=body.credentials)
    integration.status = "active"
    integration.rotated_at = func.now()  # DB clock, not app clock
    await session.flush()
    await session.refresh(integration)

    await get_audit(request).emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.INTEGRATION_CONFIGURE.value,
            resource_type="integration",
            resource_id=str(integration.id),
            # Field names only — never the credential values.
            detail={"integration_type": integration_type, "fields": sorted(body.credentials)},
        )
    )
    return ok(
        IntegrationSummary(
            integration_type=integration_type,
            status=integration.status,
            configured=True,
            rotated_at=integration.rotated_at,
        ),
        message="Integration configured.",
    )
