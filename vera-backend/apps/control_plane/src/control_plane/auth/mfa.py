"""TOTP MFA + one-time recovery codes for the password provider.

The TOTP seed is envelope-encrypted (AES-256-GCM under a per-user DEK, DEK
wrapped by a KeyManagementService) and stored directly on the `user_identity`
row. Recovery code bcrypt hashes are stored as a JSONB list on the same row.

The tenant enroll()/activate()/verify() functions mutate the `UserIdentity`
object in-place; the caller's session commits on exit from `tenant_session(...)`.
The platform variants (enroll_platform/activate_platform) instead write through
SECURITY DEFINER functions, because a NULL-tenant platform identity can't be
UPDATEd by the RLS-bound app role (migration f066c667ddc1). Platform MFA is
TOTP-only — no recovery codes (consuming one would need a definer write on an
already-enrolled row, breaking the enrollment-window guarantee).

Flow: enroll() mints and seals the seed; activate() confirms with a live code
and hands back recovery codes ONCE; verify() gates each MFA login, consuming a
recovery code on use.
"""

import secrets

import pyotp
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.password import hash_password, verify_password
from vera_core.config.kms import KeyManagementService, open_sealed, seal
from vera_core.models import UserIdentity

_RECOVERY_CODE_COUNT = 10
_ISSUER = "Vera"
_UNDEFINED_FUNCTION = "42883"  # Postgres SQLSTATE for a missing function


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


async def enroll_platform(
    kms: KeyManagementService,
    session: AsyncSession,
    *,
    identity: UserIdentity,
    account_email: str,
) -> str | None:
    """Enroll MFA for a NULL-tenant PLATFORM identity. The RLS-bound app role cannot
    UPDATE a NULL-tenant row, so the seed is written via the `platform_store_mfa_seed`
    SECURITY DEFINER function (migration f066c667ddc1) rather than through the ORM.
    Returns the provisioning URI, or None if the identity is already enrolled.

    Idempotent while unenrolled: if a seed already exists, its provisioning URI is
    returned unchanged rather than re-minting, so a second login (reload, second tab)
    can't overwrite the QR the operator already scanned."""
    existing_secret = await _decrypt_seed(kms, identity)
    if existing_secret is not None:
        return pyotp.TOTP(existing_secret).provisioning_uri(name=account_email, issuer_name=_ISSUER)
    totp_secret = pyotp.random_base32()
    seed_ct, dek_ct, key_ref = await seal(kms, totp_secret.encode())
    stored = await _call_definer_bool(
        session,
        "SELECT platform_store_mfa_seed(CAST(:id AS uuid), :seed, :dek, :ref)",
        id=identity.id,
        seed=seed_ct,
        dek=dek_ct,
        ref=key_ref,
    )
    if not stored:
        return None
    return pyotp.TOTP(totp_secret).provisioning_uri(name=account_email, issuer_name=_ISSUER)


async def activate_platform(
    kms: KeyManagementService,
    session: AsyncSession,
    *,
    identity: UserIdentity,
    code: str,
) -> bool:
    """Confirm a NULL-tenant platform enrollment with a live TOTP code, then flip
    mfa_enabled via the `platform_activate_mfa` SECURITY DEFINER function. TOTP-only —
    no recovery codes. The seed ciphertext is passed as a compare-and-set, so a seed
    re-minted by a concurrent login can't be activated against a stale QR."""
    totp_secret = await _decrypt_seed(kms, identity)
    if totp_secret is None or not pyotp.TOTP(totp_secret).verify(code, valid_window=1):
        return False
    return await _call_definer_bool(
        session,
        "SELECT platform_activate_mfa(CAST(:id AS uuid), :seed)",
        id=identity.id,
        seed=identity.totp_seed_ct,
    )


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


async def _call_definer_bool(session: AsyncSession, sql: str, **params: object) -> bool:
    """Run a platform-MFA SECURITY DEFINER function and return its boolean result. A
    missing function means the migration isn't applied yet (e.g. code shipped ahead of
    it) — surface a clear, actionable error instead of a raw UndefinedFunctionError."""
    try:
        result = await session.execute(text(sql).bindparams(**params))
    except ProgrammingError as exc:
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == _UNDEFINED_FUNCTION:
            raise RuntimeError(
                "platform MFA definer functions missing — apply migrations "
                "f066c667ddc1 and 3f7a9c2e8b41"
            ) from exc
        raise
    return bool(result.scalar_one())


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
