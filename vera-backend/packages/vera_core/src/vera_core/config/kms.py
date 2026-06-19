"""Envelope-encryption Key Management Service.

Protocol + two implementations:
  LocalDevKMS  — AES-256-GCM over LOCAL_KMS_MASTER_KEY (dev / CI only).
  GCPCloudKMS  — Cloud KMS symmetric-encrypt/decrypt (production, lazy import).

`seal` / `open_sealed` are the high-level helpers used by mfa.py: they generate
a per-call DEK, AES-256-GCM encrypt the plaintext with that DEK, and then let
the KMS wrap/unwrap the DEK. The plaintext DEK is ephemeral — it never persists.
"""

import os
import secrets
from base64 import b64decode
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from vera_core.config.settings import Settings

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_GCM_NONCE_BYTES = 12


def _gcm_encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(_GCM_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + ct


def _gcm_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    nonce, ct = ciphertext[:_GCM_NONCE_BYTES], ciphertext[_GCM_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ct, None)


@runtime_checkable
class KeyManagementService(Protocol):
    """Wrap and unwrap Data Encryption Keys (DEKs).

    The plaintext DEK is always ephemeral — callers must not persist it.
    `key_version_ref` is an opaque string stored alongside the wrapped DEK
    so audit and rotation logic can identify which key version was used.
    """

    async def wrap_dek(self, plaintext_dek: bytes) -> tuple[bytes, str]:
        """Encrypt `plaintext_dek`; return (wrapped_dek, key_version_ref)."""
        ...

    async def unwrap_dek(self, wrapped_dek: bytes, key_version_ref: str) -> bytes:
        """Decrypt `wrapped_dek`; return the plaintext DEK."""
        ...


class LocalDevKMS:
    """Dev / CI: wraps DEKs with AES-256-GCM using a master key from env.

    Pass `master_key` directly to bypass the env-var lookup (test injection).
    Set `LOCAL_KMS_MASTER_KEY` to a base64-encoded 32-byte value for local dev:
      python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
    """

    def __init__(self, master_key: bytes | None = None) -> None:
        if master_key is not None:
            self._key = master_key
        else:
            raw = os.environ.get("LOCAL_KMS_MASTER_KEY")
            if raw is None:
                raise ValueError(
                    "LOCAL_KMS_MASTER_KEY env var must be set when kms_key_name is unset"
                )
            self._key = b64decode(raw)
        if len(self._key) != 32:
            raise ValueError(
                f"KMS master key must decode to exactly 32 bytes, got {len(self._key)}"
            )

    async def wrap_dek(self, plaintext_dek: bytes) -> tuple[bytes, str]:
        return _gcm_encrypt(self._key, plaintext_dek), "local:1"

    async def unwrap_dek(self, wrapped_dek: bytes, key_version_ref: str) -> bytes:
        # key_version_ref is informational here: local dev has a single master key.
        return _gcm_decrypt(self._key, wrapped_dek)


class GCPCloudKMS:
    """Production: Cloud KMS symmetric encrypt/decrypt via Workload Identity on GKE.

    `key_name` is the full resource path:
      projects/{project}/locations/{location}/keyRings/{ring}/cryptoKeys/{key}

    `wrap_dek` returns the full key *version* path returned by Cloud KMS as the
    key_version_ref — stored on the row for audit and rotation tracking.
    `unwrap_dek` passes the key name (not version) to Cloud KMS decrypt, which
    automatically selects the correct version from ciphertext metadata.

    TODO(ops): add GOOGLE_APPLICATION_CREDENTIALS / Workload Identity binding
    in the GKE service account IAM — see adr/devops-todo.md.
    """

    def __init__(self, key_name: str) -> None:
        self._key_name = key_name
        self._client: Any = None

    def _kms_client(self) -> Any:
        # Cache the gRPC client across calls — channel/TLS setup is expensive and
        # the async client is designed to be long-lived. Lazily imported so dev/CI
        # (LocalDevKMS) never needs google-cloud-kms installed.
        if self._client is None:
            from google.cloud import kms

            self._client = kms.KeyManagementServiceAsyncClient()
        return self._client

    async def wrap_dek(self, plaintext_dek: bytes) -> tuple[bytes, str]:
        response = await self._kms_client().encrypt(
            request={"name": self._key_name, "plaintext": plaintext_dek}
        )
        return response.ciphertext, response.name  # name is the full key version path

    async def unwrap_dek(self, wrapped_dek: bytes, key_version_ref: str) -> bytes:
        response = await self._kms_client().decrypt(
            request={"name": self._key_name, "ciphertext": wrapped_dek}
        )
        return bytes(response.plaintext)


async def seal(kms: KeyManagementService, plaintext: bytes) -> tuple[bytes, bytes, str]:
    """Envelope-encrypt `plaintext`.

    Generates a fresh 32-byte DEK, encrypts `plaintext` with AES-256-GCM,
    then wraps the DEK with `kms`. Returns (seed_ct, dek_ct, key_version_ref).
    The DEK is not retained after this call.
    """
    dek = secrets.token_bytes(32)
    seed_ct = _gcm_encrypt(dek, plaintext)
    dek_ct, key_ref = await kms.wrap_dek(dek)
    return seed_ct, dek_ct, key_ref


async def open_sealed(
    kms: KeyManagementService,
    seed_ct: bytes,
    dek_ct: bytes,
    key_version_ref: str,
) -> bytes:
    """Reverse of `seal`: unwrap the DEK, decrypt the ciphertext, return plaintext."""
    dek = await kms.unwrap_dek(dek_ct, key_version_ref)
    return _gcm_decrypt(dek, seed_ct)


def build_kms(settings: "Settings") -> KeyManagementService:
    """Factory: GCPCloudKMS when `kms_key_name` is set, else LocalDevKMS."""
    if settings.kms_key_name is not None:
        return GCPCloudKMS(key_name=settings.kms_key_name)
    return LocalDevKMS()
