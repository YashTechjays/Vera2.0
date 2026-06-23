"""Seal / open a tenant integration's outbound credential with envelope encryption.

Mirrors the MFA scheme (control_plane/auth/mfa.py): the credential JSON is
AES-256-GCM encrypted under a fresh per-row DEK, the DEK is wrapped by the
KeyManagementService, and the three resulting values are stored on the
`integration` row (`credential_ct`, `dek_ct`, `secret_ref`). The plaintext
credential and the plaintext DEK are never persisted.

Pure helpers: they mutate the passed `Integration` in place and read its fields;
the caller owns the SQLAlchemy session and commits. Credentials here are
infrastructure secrets (e.g. a Twilio Elastic SIP trunk id), not PHI.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.config.kms import KeyManagementService, open_sealed, seal
from vera_core.models import Integration, IntegrationType


async def seal_credentials(
    kms: KeyManagementService, *, integration: Integration, credentials: dict[str, Any]
) -> None:
    """Envelope-encrypt `credentials` (e.g. {"twilio_sip_trunk": "ST.."}) onto the row.

    Validate the shape against `integration_type.credentials_schema` before calling.
    """
    plaintext = json.dumps(credentials, separators=(",", ":"), sort_keys=True).encode()
    credential_ct, dek_ct, key_ref = await seal(kms, plaintext)
    integration.credential_ct = credential_ct
    integration.dek_ct = dek_ct
    integration.secret_ref = key_ref


async def open_credentials(
    kms: KeyManagementService, *, integration: Integration
) -> dict[str, Any] | None:
    """Reverse of `seal_credentials`: return the decrypted credential dict, or None
    if the row carries no sealed credential yet."""
    if (
        integration.credential_ct is None
        or integration.dek_ct is None
        or integration.secret_ref is None
    ):
        return None
    plaintext = await open_sealed(
        kms, integration.credential_ct, integration.dek_ct, integration.secret_ref
    )
    result: dict[str, Any] = json.loads(plaintext)
    return result


async def get_integration_credentials(
    session: AsyncSession,
    kms: KeyManagementService,
    *,
    integration_type_name: str,
) -> dict[str, Any] | None:
    """Load + decrypt the calling tenant's credential for an integration type by name
    (e.g. "twilio_sip"). Returns None if the tenant hasn't configured it. The session
    must be tenant-scoped (RLS) so only the caller's own row is visible."""
    integration = (
        await session.execute(
            select(Integration)
            .join(IntegrationType, IntegrationType.id == Integration.integration_type_id)
            .where(IntegrationType.name == integration_type_name)
        )
    ).scalar_one_or_none()
    if integration is None:
        return None
    return await open_credentials(kms, integration=integration)
