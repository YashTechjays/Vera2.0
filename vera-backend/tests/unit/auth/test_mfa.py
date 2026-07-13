"""Unit tests for TOTP MFA + one-time recovery codes.

All three MFA operations (enroll, activate, verify) mutate a UserIdentity
object in-place. These tests construct the identity in-memory — no DB needed.
"""

import time
from unittest.mock import patch

import pyotp
import pytest

from control_plane.auth import mfa
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import UserIdentity
from vera_core.models.enums import ProviderKind

_KMS = LocalDevKMS(master_key=b"a" * 32)


def _identity() -> UserIdentity:
    uid = uuid7()
    return UserIdentity(
        tenant_id=uid,
        app_user_id=uid,
        provider_type=ProviderKind.PASSWORD.value,
        email="a@example.com",
    )


async def _enrolled_secret(identity: UserIdentity) -> str:
    """Enroll and return the base32 TOTP secret extracted from the provisioning URI."""
    uri = await mfa.enroll(_KMS, identity=identity, account_email="a@example.com")
    return pyotp.parse_uri(uri).secret


@pytest.mark.asyncio
async def test_enroll_returns_provisioning_uri() -> None:
    identity = _identity()
    uri = await mfa.enroll(_KMS, identity=identity, account_email="a@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "Vera" in uri


@pytest.mark.asyncio
async def test_enroll_stores_encrypted_seed_on_identity() -> None:
    identity = _identity()
    await mfa.enroll(_KMS, identity=identity, account_email="a@example.com")
    assert identity.totp_seed_ct is not None
    assert identity.totp_dek_ct is not None
    assert identity.totp_key_ref is not None


@pytest.mark.asyncio
async def test_activate_with_valid_code_returns_ten_distinct_recovery_codes() -> None:
    identity = _identity()
    secret = await _enrolled_secret(identity)
    codes = await mfa.activate(_KMS, identity=identity, code=pyotp.TOTP(secret).now())
    assert codes is not None
    assert len(codes) == 10
    assert len(set(codes)) == 10


@pytest.mark.asyncio
async def test_activate_stores_hashes_on_identity() -> None:
    identity = _identity()
    secret = await _enrolled_secret(identity)
    await mfa.activate(_KMS, identity=identity, code=pyotp.TOTP(secret).now())
    assert identity.recovery_code_hashes is not None
    assert len(identity.recovery_code_hashes) == 10


@pytest.mark.asyncio
async def test_activate_with_invalid_code_returns_none() -> None:
    identity = _identity()
    await _enrolled_secret(identity)
    result = await mfa.activate(_KMS, identity=identity, code="000000")
    assert result is None


@pytest.mark.asyncio
async def test_verify_accepts_current_totp() -> None:
    identity = _identity()
    secret = await _enrolled_secret(identity)
    await mfa.activate(_KMS, identity=identity, code=pyotp.TOTP(secret).now())
    assert await mfa.verify(_KMS, identity=identity, code=pyotp.TOTP(secret).now())


@pytest.mark.asyncio
async def test_verify_rejects_wrong_code() -> None:
    identity = _identity()
    secret = await _enrolled_secret(identity)
    totp = pyotp.TOTP(secret)
    await mfa.activate(_KMS, identity=identity, code=totp.now())
    # Freeze time and compute what the valid code IS, then submit anything else.
    frozen_step = 1000
    valid_code = totp.at(for_time=frozen_step * 30)
    # Pick a code that's definitely not the valid one for this (or adjacent) timestep.
    wrong_code = "000001" if valid_code != "000001" else "000002"
    with patch("control_plane.auth.mfa._current_timestep", return_value=frozen_step):
        assert await mfa.verify(_KMS, identity=identity, code=wrong_code) is None


@pytest.mark.asyncio
async def test_recovery_code_is_one_time() -> None:
    identity = _identity()
    secret = await _enrolled_secret(identity)
    codes = await mfa.activate(_KMS, identity=identity, code=pyotp.TOTP(secret).now())
    assert codes is not None
    recovery = codes[0]
    assert await mfa.verify(_KMS, identity=identity, code=recovery)
    assert not await mfa.verify(_KMS, identity=identity, code=recovery)


@pytest.mark.asyncio
async def test_verify_returns_false_when_no_seed_enrolled() -> None:
    identity = _identity()
    assert not await mfa.verify(_KMS, identity=identity, code="123456")


@pytest.mark.asyncio
async def test_totp_replay_is_rejected_on_same_step() -> None:
    """A TOTP code accepted once must be rejected on immediate re-submit (same timestep)."""
    identity = _identity()
    secret = await _enrolled_secret(identity)
    await mfa.activate(_KMS, identity=identity, code=pyotp.TOTP(secret).now())

    # Freeze time so both verify calls land in the same 30s window.
    frozen_step = int(time.time()) // 30
    with patch("control_plane.auth.mfa._current_timestep", return_value=frozen_step):
        code = pyotp.TOTP(secret).at(frozen_step * 30)
        first = await mfa.verify(_KMS, identity=identity, code=code)
        second = await mfa.verify(_KMS, identity=identity, code=code)

    assert first is not None  # accepted — returns matched timestep (int)
    assert second is None  # rejected replay


@pytest.mark.asyncio
async def test_totp_fresh_step_accepted_after_prior_step_consumed() -> None:
    """After consuming one step, the next step's code is accepted."""
    identity = _identity()
    secret = await _enrolled_secret(identity)
    await mfa.activate(_KMS, identity=identity, code=pyotp.TOTP(secret).now())

    frozen_ts_1 = 1000 * 30  # timestep 1000
    frozen_ts_2 = 1001 * 30  # timestep 1001

    with patch("control_plane.auth.mfa._current_timestep", return_value=1000):
        code1 = pyotp.TOTP(secret).at(frozen_ts_1)
        first = await mfa.verify(_KMS, identity=identity, code=code1)
    assert first is not None  # accepted
    assert identity.totp_last_used_timestep == 1000

    with patch("control_plane.auth.mfa._current_timestep", return_value=1001):
        code2 = pyotp.TOTP(secret).at(frozen_ts_2)
        second = await mfa.verify(_KMS, identity=identity, code=code2)
    assert second is not None  # accepted
    assert identity.totp_last_used_timestep == 1001
