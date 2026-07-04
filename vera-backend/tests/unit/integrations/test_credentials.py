"""Unit tests for integration credential envelope encryption (DB-free)."""

import secrets

import pytest

from vera_core.config.kms import LocalDevKMS
from vera_core.integrations.credentials import open_credentials, seal_credentials
from vera_core.models import Integration


def _kms() -> LocalDevKMS:
    return LocalDevKMS(master_key=secrets.token_bytes(32))


@pytest.mark.asyncio
async def test_seal_then_open_roundtrips() -> None:
    kms = _kms()
    integration = Integration()
    creds = {"trunk_id": "ST0123456789abcdef0123456789abcdef"}

    await seal_credentials(kms, integration=integration, credentials=creds)

    # All three sealed values are populated; plaintext never lands on the row.
    assert integration.credential_ct is not None
    assert integration.dek_ct is not None
    assert integration.secret_ref == "local:1"
    assert b"trunk_id" not in integration.credential_ct
    assert b"ST0123456789" not in integration.credential_ct

    assert await open_credentials(kms, integration=integration) == creds


@pytest.mark.asyncio
async def test_open_returns_none_when_unset() -> None:
    assert await open_credentials(_kms(), integration=Integration()) is None


@pytest.mark.asyncio
async def test_each_seal_uses_a_fresh_dek() -> None:
    kms = _kms()
    a, b = Integration(), Integration()
    creds = {"trunk_id": "STsame"}

    await seal_credentials(kms, integration=a, credentials=creds)
    await seal_credentials(kms, integration=b, credentials=creds)

    # Same plaintext, different DEK + nonce → different ciphertext each time.
    assert a.credential_ct != b.credential_ct
    assert a.dek_ct != b.dek_ct
    assert await open_credentials(kms, integration=a) == creds
    assert await open_credentials(kms, integration=b) == creds
