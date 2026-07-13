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
import time
from uuid import UUID

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
_RECOVERY_SENTINEL = -1  # verify() return value for "recovery code accepted, no timestep"


def _current_timestep() -> int:
    """Current TOTP timestep (30-second window). Extracted for test patching."""
    return int(time.time() // 30)


def _provisioning_uri(totp_secret: str, account_email: str) -> str:
    return pyotp.TOTP(totp_secret).provisioning_uri(name=account_email, issuer_name=_ISSUER)


async def enroll(kms: KeyManagementService, *, identity: UserIdentity, account_email: str) -> str:
    """Mint a fresh TOTP seed, envelope-encrypt it onto `identity`, return provisioning URI."""
    totp_secret = pyotp.random_base32()
    seed_ct, dek_ct, key_ref = await seal(kms, totp_secret.encode())
    identity.totp_seed_ct = seed_ct
    identity.totp_dek_ct = dek_ct
    identity.totp_key_ref = key_ref
    identity.recovery_code_hashes = []
    return _provisioning_uri(totp_secret, account_email)


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
    Idempotent while unenrolled: an existing seed's URI is returned rather than
    re-minting, so a repeat login can't overwrite the QR already scanned."""
    existing_secret = await _decrypt_seed(kms, identity)
    if existing_secret is not None:
        return _provisioning_uri(existing_secret, account_email)
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
    return _provisioning_uri(totp_secret, account_email)


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


async def verify(kms: KeyManagementService, *, identity: UserIdentity, code: str) -> int | None:
    """Gate an MFA login: accept a current TOTP code (with ±1 window drift
    tolerance), or consume a one-time recovery code. Each TOTP code is single-use
    within its drift window — a replayed code in the same timestep is rejected.

    Returns:
        int >= 0          — TOTP accepted; the matched timestep (caller may persist it for
                            replay protection, e.g. via platform_update_totp_last_used).
        _RECOVERY_SENTINEL — recovery code accepted (timestep tracking does not apply;
                            recovery codes are already consumed in-place on ``identity``).
        None              — rejected (wrong code, replay, or no seed enrolled).
    """
    totp_secret = await _decrypt_seed(kms, identity)
    if totp_secret is None:
        return None
    totp = pyotp.TOTP(totp_secret)
    # Find which of the three ±1-window timesteps (current, prev, next) matches the
    # submitted code, so we can enforce single-use per timestep.
    now_step = _current_timestep()
    matched_step: int | None = next(
        (now_step + o for o in (0, -1, 1) if totp.at(for_time=(now_step + o) * 30) == code),
        None,
    )
    if matched_step is not None:
        last = identity.totp_last_used_timestep
        if last is not None and matched_step <= last:
            return None  # replay within the drift window
        identity.totp_last_used_timestep = matched_step
        return matched_step
    hashes: list[str] = identity.recovery_code_hashes or []
    for i, hashed in enumerate(hashes):
        if verify_password(code, hashed):
            identity.recovery_code_hashes = hashes[:i] + hashes[i + 1 :]
            return _RECOVERY_SENTINEL
    return None


async def platform_update_totp_last_used(
    session: AsyncSession, *, identity_id: UUID, step: int
) -> None:
    """Persist the matched TOTP timestep for a NULL-tenant platform identity via
    SECURITY DEFINER (RLS-bound role cannot UPDATE NULL-tenant rows directly)."""
    await _call_definer_void(
        session,
        "SELECT platform_update_totp_last_used(CAST(:id AS uuid), :step)",
        id=identity_id,
        step=step,
    )


async def _call_definer_bool(session: AsyncSession, sql: str, **params: object) -> bool:
    """Run a platform-MFA SECURITY DEFINER function → bool. A missing function (migration
    not applied yet) surfaces a clear error, not a raw UndefinedFunctionError."""
    try:
        result = await session.execute(text(sql).bindparams(**params))
    except ProgrammingError as exc:
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == _UNDEFINED_FUNCTION:
            raise RuntimeError(
                "platform MFA definer functions missing — apply migrations "
                "f066c667ddc1, 3f7a9c2e8b41, and 8fcbca449f35"
            ) from exc
        raise
    return bool(result.scalar_one())


async def _call_definer_void(session: AsyncSession, sql: str, **params: object) -> None:
    """Run a platform-MFA SECURITY DEFINER function that returns void. A missing function
    (migration not yet applied) surfaces a clear error, not a raw UndefinedFunctionError."""
    try:
        await session.execute(text(sql).bindparams(**params))
    except ProgrammingError as exc:
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == _UNDEFINED_FUNCTION:
            raise RuntimeError(
                "platform MFA definer functions missing — apply migrations "
                "f066c667ddc1, 3f7a9c2e8b41, and 8fcbca449f35"
            ) from exc
        raise


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
