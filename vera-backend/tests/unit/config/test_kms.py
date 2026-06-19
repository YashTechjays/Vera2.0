"""Unit tests for KMS protocol implementations."""

import secrets

import pytest

from vera_core.config.kms import LocalDevKMS, open_sealed, seal

_KEY = b"a" * 32
_KMS = LocalDevKMS(master_key=_KEY)


@pytest.mark.asyncio
async def test_local_dev_kms_wrap_unwrap_roundtrip() -> None:
    plaintext_dek = secrets.token_bytes(32)
    wrapped, ref = await _KMS.wrap_dek(plaintext_dek)
    assert wrapped != plaintext_dek
    assert ref == "local:1"
    recovered = await _KMS.unwrap_dek(wrapped, ref)
    assert recovered == plaintext_dek


@pytest.mark.asyncio
async def test_local_dev_kms_wrap_produces_different_ciphertext_each_call() -> None:
    dek = secrets.token_bytes(32)
    wrapped1, _ = await _KMS.wrap_dek(dek)
    wrapped2, _ = await _KMS.wrap_dek(dek)
    assert wrapped1 != wrapped2  # fresh nonce each time


@pytest.mark.asyncio
async def test_seal_open_sealed_roundtrip() -> None:
    plaintext = b"JBSWY3DPEHPK3PXP"  # base32 seed
    seed_ct, dek_ct, key_ref = await seal(_KMS, plaintext)
    assert seed_ct != plaintext
    recovered = await open_sealed(_KMS, seed_ct, dek_ct, key_ref)
    assert recovered == plaintext


def test_local_dev_kms_rejects_short_master_key() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        LocalDevKMS(master_key=b"too-short")


def test_local_dev_kms_rejects_missing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_KMS_MASTER_KEY", raising=False)
    with pytest.raises(ValueError, match="LOCAL_KMS_MASTER_KEY"):
        LocalDevKMS()
