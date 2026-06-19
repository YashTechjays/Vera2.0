"""TOTP MFA + one-time recovery codes for the password provider.

The TOTP seed is envelope-encrypted (AES-256-GCM under a per-user DEK, DEK
wrapped by a KeyManagementService) and stored directly on the `user_identity`
row. Recovery code bcrypt hashes are stored as a JSONB list on the same row.

All three functions mutate the `UserIdentity` object in-place. The caller's
SQLAlchemy session tracks the changes and commits them on exit from the
`async with tenant_session(...)` block — mfa.py never opens a DB session.

Flow: enroll() mints and seals the seed; activate() confirms with a live code
and hands back recovery codes ONCE; verify() gates each MFA login, consuming a
recovery code on use.
"""

import secrets

import pyotp

from control_plane.auth.password import hash_password, verify_password
from vera_core.config.kms import KeyManagementService, open_sealed, seal
from vera_core.models import UserIdentity

_RECOVERY_CODE_COUNT = 10
_ISSUER = "Vera"


async def enroll(kms: KeyManagementService, *, identity: UserIdentity, account_email: str) -> str:
    """Mint a fresh TOTP seed, envelope-encrypt it onto `identity`, return provisioning URI."""
    totp_secret = pyotp.random_base32()
    seed_ct, dek_ct, key_ref = await seal(kms, totp_secret.encode())
    identity.totp_seed_ct = seed_ct
    identity.totp_dek_ct = dek_ct
    identity.totp_key_ref = key_ref
    identity.recovery_code_hashes = []
    return pyotp.TOTP(totp_secret).provisioning_uri(name=account_email, issuer_name=_ISSUER)


async def activate(
    kms: KeyManagementService, *, identity: UserIdentity, code: str
) -> tuple[str, ...] | None:
    """Confirm enrollment with a live TOTP code. On success, generate one-time recovery
    codes, store their hashes on `identity`, and return the plaintext codes ONCE.
    Returns None if the code is invalid (caller must not set mfa_enabled)."""
    totp_secret = await _decrypt_seed(kms, identity)
    if totp_secret is None or not pyotp.TOTP(totp_secret).verify(code, valid_window=1):
        return None
    recovery_codes = tuple(secrets.token_hex(5) for _ in range(_RECOVERY_CODE_COUNT))
    identity.recovery_code_hashes = [hash_password(c) for c in recovery_codes]
    return recovery_codes


async def verify(kms: KeyManagementService, *, identity: UserIdentity, code: str) -> bool:
    """Gate an MFA login: accept a current TOTP code, or consume a one-time recovery code."""
    totp_secret = await _decrypt_seed(kms, identity)
    if totp_secret is None:
        return False
    if pyotp.TOTP(totp_secret).verify(code, valid_window=1):
        return True
    hashes: list[str] = identity.recovery_code_hashes or []
    for i, hashed in enumerate(hashes):
        if verify_password(code, hashed):
            identity.recovery_code_hashes = hashes[:i] + hashes[i + 1 :]
            return True
    return False


async def _decrypt_seed(kms: KeyManagementService, identity: UserIdentity) -> str | None:
    if (
        identity.totp_seed_ct is None
        or identity.totp_dek_ct is None
        or identity.totp_key_ref is None
    ):
        return None
    seed_bytes = await open_sealed(
        kms, identity.totp_seed_ct, identity.totp_dek_ct, identity.totp_key_ref
    )
    return seed_bytes.decode()
