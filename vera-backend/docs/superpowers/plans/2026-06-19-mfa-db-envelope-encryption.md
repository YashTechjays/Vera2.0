# MFA DB Envelope Encryption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `WritableSecretProvider`-backed TOTP seed store with database-resident envelope encryption: the TOTP seed is AES-256-GCM encrypted under a per-user DEK, the DEK is wrapped by a `KeyManagementService` (local AES master key in dev, Google Cloud KMS in production), and bcrypt recovery code hashes are stored as JSONB on `user_identity`.

**Architecture:** A new `vera_core/config/kms.py` defines a `KeyManagementService` protocol with two implementations: `LocalDevKMS` (AES-256-GCM over a master key from `LOCAL_KMS_MASTER_KEY` env var) and `GCPCloudKMS` (delegates wrap/unwrap to Cloud KMS, lazy-imports `google-cloud-kms`). A `build_kms(settings)` factory picks the implementation based on whether `settings.kms_key_name` is set. `mfa.py` is rewritten to be fully async, operating directly on the `UserIdentity` ORM row (no more `WritableSecretProvider`, no more opaque ref key). `user_identity` gains four columns (`totp_seed_ct`, `totp_dek_ct`, `totp_key_ref`, `recovery_code_hashes`) and loses `mfa_secret_ref`.

**Tech Stack:** Python 3.12, SQLAlchemy async, `cryptography>=44` (AES-256-GCM), `pyotp`, `bcrypt`, `google-cloud-kms>=2.21` (prod only, lazy import), FastAPI, Alembic, pytest-asyncio.

## Global Constraints

- Python `<3.13`; use PEP 695 type params (`class Foo[T]`, `def f[T]`) — ruff rejects `Generic[T]`/`TypeVar`.
- Async runtime is `asyncio` only — never `anyio`, never `asyncio.run()` inside library code.
- DB timestamps via `func.now()` only — never `datetime.now()`.
- `just check` must pass (ruff + mypy --strict + pytest) before every commit.
- After the final task, run `/simplify` on the full diff before claiming done.
- PHI guardrails: TOTP seeds and recovery code hashes are credentials, not PHI — but they must never be logged, traced, or returned in API responses.
- Migration `0009` follows the same pattern as `0008`: `ADD COLUMN IF NOT EXISTS`, no backfill, pre-launch DB only.
- Each task ends with a `git commit`.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| **Create** | `packages/vera_core/src/vera_core/config/kms.py` | `KeyManagementService` protocol, `LocalDevKMS`, `GCPCloudKMS`, `build_kms`, `seal`, `open_sealed` |
| **Create** | `tests/unit/config/test_kms.py` | Unit tests for `LocalDevKMS` round-trip and `seal`/`open_sealed` |
| **Create** | `migrations/versions/0009_mfa_db_envelope.py` | Drop `mfa_secret_ref`, add 4 new MFA columns |
| **Modify** | `packages/vera_core/src/vera_core/config/settings.py` | Add `kms_key_name: str \| None = None` |
| **Modify** | `packages/vera_core/src/vera_core/config/secrets.py` | Remove `WritableSecretProvider`, `InMemorySecretProvider`, `GoogleSecretManagerProvider` |
| **Modify** | `packages/vera_core/src/vera_core/config/__init__.py` | Export KMS symbols; drop removed secret-provider symbols |
| **Modify** | `packages/vera_core/src/vera_core/models/auth.py` | Swap `mfa_secret_ref` for `totp_seed_ct`, `totp_dek_ct`, `totp_key_ref`, `recovery_code_hashes` |
| **Modify** | `packages/vera_core/pyproject.toml` | Add `cryptography>=44` |
| **Modify** | `apps/control_plane/src/control_plane/auth/mfa.py` | Rewrite: async, `KeyManagementService`-based, mutates `UserIdentity` in place |
| **Modify** | `apps/control_plane/src/control_plane/main.py` | Add `kms` param, wire `build_kms(settings)`, remove `secret_provider` |
| **Modify** | `apps/control_plane/src/control_plane/deps.py` | Add `get_kms`; remove `get_secret_provider` |
| **Modify** | `apps/control_plane/src/control_plane/api/v1/auth.py` | Swap `secret_provider` for `kms` in all MFA endpoints; remove `SecretNotFoundError` usage |
| **Modify** | `apps/control_plane/pyproject.toml` | Add `google-cloud-kms>=2.21` (prod dep) |
| **Modify** | `tests/unit/auth/test_mfa.py` | Rewrite for new async `UserIdentity`-based interface |
| **Modify** | `tests/integration/control_plane/test_login_flow.py` | Swap `InMemorySecretProvider` for `LocalDevKMS(master_key=b"a"*32)` |
| **Modify** | `adr/devops-todo.md` | Add GCP Cloud KMS infra obligation row |
| **Modify** | `CLAUDE.md` (repo root) | Note KMS abstraction + `LOCAL_KMS_MASTER_KEY` |
| **Modify** | `packages/vera_core/src/vera_core/CLAUDE.md` | Envelope encryption pattern + KMS boundary |
| **Modify** | `apps/control_plane/src/control_plane/CLAUDE.md` | KMS dep injection + `build_kms` factory |

---

## Task 1: `vera_core/config/kms.py` — KMS Protocol + Implementations

**Files:**
- Create: `packages/vera_core/src/vera_core/config/kms.py`
- Modify: `packages/vera_core/src/vera_core/config/settings.py` (add `kms_key_name`)
- Modify: `packages/vera_core/pyproject.toml` (add `cryptography>=44`)
- Test: `tests/unit/config/test_kms.py`

**Interfaces:**
- Produces:
  - `KeyManagementService` — Protocol with `wrap_dek(plaintext_dek: bytes) -> tuple[bytes, str]` and `unwrap_dek(wrapped_dek: bytes, key_version_ref: str) -> bytes` (both `async`)
  - `LocalDevKMS(master_key: bytes | None = None)` — reads `LOCAL_KMS_MASTER_KEY` env var when `master_key` is `None`
  - `GCPCloudKMS(key_name: str)` — delegates to `google-cloud-kms`
  - `build_kms(settings: Settings) -> KeyManagementService`
  - `seal(kms: KeyManagementService, plaintext: bytes) -> tuple[bytes, bytes, str]` — returns `(seed_ct, dek_ct, key_ref)`
  - `open_sealed(kms: KeyManagementService, seed_ct: bytes, dek_ct: bytes, key_ref: str) -> bytes`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/config/test_kms.py
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /path/to/repo && python -m pytest tests/unit/config/test_kms.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `vera_core.config.kms` does not exist yet.

- [ ] **Step 3: Add `cryptography>=44` to `vera_core` deps**

In `packages/vera_core/pyproject.toml`, in the `[project]` `dependencies` list, add:

```toml
"cryptography>=44",
```

- [ ] **Step 4: Create `kms.py`**

```python
# packages/vera_core/src/vera_core/config/kms.py
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
from typing import Protocol, runtime_checkable

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
            raise ValueError(f"KMS master key must decode to exactly 32 bytes, got {len(self._key)}")

    async def wrap_dek(self, plaintext_dek: bytes) -> tuple[bytes, str]:
        return _gcm_encrypt(self._key, plaintext_dek), "local:1"

    async def unwrap_dek(self, wrapped_dek: bytes, key_version_ref: str) -> bytes:
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

    async def wrap_dek(self, plaintext_dek: bytes) -> tuple[bytes, str]:
        from google.cloud import kms  # type: ignore[import-untyped]

        client = kms.KeyManagementServiceAsyncClient()
        response = await client.encrypt(
            request={"name": self._key_name, "plaintext": plaintext_dek}
        )
        return response.ciphertext, response.name  # name is the full key version path

    async def unwrap_dek(self, wrapped_dek: bytes, key_version_ref: str) -> bytes:
        from google.cloud import kms  # type: ignore[import-untyped]

        client = kms.KeyManagementServiceAsyncClient()
        response = await client.decrypt(
            request={"name": self._key_name, "ciphertext": wrapped_dek}
        )
        return response.plaintext


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
```

- [ ] **Step 5: Add `kms_key_name` to `Settings`**

In `packages/vera_core/src/vera_core/config/settings.py`, after the `gcp_project` line, add:

```python
    # --- KMS ------------------------------------------------------------------
    # Full Cloud KMS resource path for production MFA envelope encryption:
    #   projects/{project}/locations/{location}/keyRings/{ring}/cryptoKeys/{key}
    # Unset → LocalDevKMS (requires LOCAL_KMS_MASTER_KEY env var).
    # Set   → GCPCloudKMS (requires Workload Identity or GOOGLE_APPLICATION_CREDENTIALS).
    kms_key_name: str | None = None
```

- [ ] **Step 6: Add `build_kms` to `kms.py`**

At the bottom of `packages/vera_core/src/vera_core/config/kms.py`, after the `open_sealed` function, add:

```python
def build_kms(settings: "Settings") -> KeyManagementService:
    """Factory: pick KMS implementation from settings.

    If `kms_key_name` is set, use GCPCloudKMS (production).
    Otherwise, use LocalDevKMS (reads LOCAL_KMS_MASTER_KEY from env).
    """
    from vera_core.config.settings import Settings  # local import avoids circular dep

    if settings.kms_key_name is not None:
        return GCPCloudKMS(key_name=settings.kms_key_name)
    return LocalDevKMS()
```

Note: `Settings` is imported locally inside the function to avoid a circular import (`kms.py` → `settings.py` → `kms.py` is not yet a problem, but the local import is explicit about the dependency direction).

Actually, to avoid the circular import concern entirely, define `build_kms` without a type annotation on `settings`:

```python
def build_kms(settings: object) -> KeyManagementService:
    """Factory: pick KMS implementation from settings (accepts any Settings-shaped object)."""
    kms_key_name: str | None = getattr(settings, "kms_key_name", None)
    if kms_key_name is not None:
        return GCPCloudKMS(key_name=kms_key_name)
    return LocalDevKMS()
```

- [ ] **Step 7: Run tests — expect them to pass**

```bash
python -m pytest tests/unit/config/test_kms.py -v
```

Expected output: all 5 tests `PASSED`.

- [ ] **Step 8: Run `just check`**

```bash
just check
```

Expected: PASS (lint + type + existing tests).

- [ ] **Step 9: Commit**

```bash
git add packages/vera_core/src/vera_core/config/kms.py \
        packages/vera_core/src/vera_core/config/settings.py \
        packages/vera_core/pyproject.toml \
        tests/unit/config/test_kms.py
git commit -m "feat(kms): add KeyManagementService protocol, LocalDevKMS, GCPCloudKMS, seal/open_sealed"
```

---

## Task 2: DB Model + Migration

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/auth.py`
- Create: `migrations/versions/0009_mfa_db_envelope.py`

**Interfaces:**
- Consumes: `packages/vera_core/src/vera_core/config/kms.py` (Task 1 — no direct import, but the columns store its outputs)
- Produces: `UserIdentity` with new columns:
  - `totp_seed_ct: Mapped[bytes | None]` — AES-256-GCM ciphertext of the base32 TOTP seed
  - `totp_dek_ct: Mapped[bytes | None]` — KMS-wrapped DEK bytes
  - `totp_key_ref: Mapped[str | None]` — KMS key version reference for rotation/audit
  - `recovery_code_hashes: Mapped[list[str] | None]` — JSONB array of bcrypt hashes
  - `mfa_secret_ref` removed

- [ ] **Step 1: Update `UserIdentity` in `auth.py`**

In `packages/vera_core/src/vera_core/models/auth.py`, replace the `mfa_secret_ref` line:

```python
    mfa_secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
```

With these four columns:

```python
    totp_seed_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_dek_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_key_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    recovery_code_hashes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
```

`LargeBinary` and `JSONB` are already imported at the top of the file.

- [ ] **Step 2: Write migration `0009_mfa_db_envelope.py`**

```python
# migrations/versions/0009_mfa_db_envelope.py
"""MFA material moves from WritableSecretProvider into the DB with envelope encryption.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-19

Replaces the opaque `mfa_secret_ref` pointer (which referenced an external
secret store) with four columns that hold the MFA material directly:

  totp_seed_ct         bytea   AES-256-GCM ciphertext of the base32 TOTP seed
  totp_dek_ct          bytea   KMS-wrapped Data Encryption Key
  totp_key_ref         varchar KMS key version reference (for rotation/audit)
  recovery_code_hashes jsonb   Array of bcrypt hashes of unused recovery codes

Pre-launch with no data: columns are added NOT NULL-free (nullable), no
backfill required. A plain `ADD COLUMN IF NOT EXISTS` is the sanctioned
pattern (see 0008 module docstring).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS mfa_secret_ref")
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS totp_seed_ct bytea")
    op.execute("ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS totp_dek_ct bytea")
    op.execute(
        "ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS totp_key_ref varchar(512)"
    )
    op.execute(
        "ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS recovery_code_hashes jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS recovery_code_hashes")
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS totp_key_ref")
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS totp_dek_ct")
    op.execute("ALTER TABLE user_identity DROP COLUMN IF EXISTS totp_seed_ct")
    op.execute(
        "ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS mfa_secret_ref varchar(512)"
    )
```

- [ ] **Step 3: Apply the migration**

```bash
just migrate
```

Expected: migration `0009` runs without error; Alembic stamps `0009` as current.

- [ ] **Step 4: Run `just check`**

```bash
just check
```

Expected: PASS. (The old `mfa_secret_ref` references in `mfa.py` and `auth.py` will now be mypy errors — that is expected and will be fixed in Tasks 3 and 4.)

If mypy reports errors about `mfa_secret_ref`, note them and proceed — they are resolved in Task 3.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/models/auth.py \
        migrations/versions/0009_mfa_db_envelope.py
git commit -m "feat(db): swap mfa_secret_ref for envelope-encryption columns on user_identity"
```

---

## Task 3: Rewrite `mfa.py`

**Files:**
- Modify: `apps/control_plane/src/control_plane/auth/mfa.py`
- Modify: `tests/unit/auth/test_mfa.py`

**Interfaces:**
- Consumes:
  - `KeyManagementService` from `vera_core.config.kms`
  - `seal(kms, plaintext) -> tuple[bytes, bytes, str]` from `vera_core.config.kms`
  - `open_sealed(kms, seed_ct, dek_ct, key_ref) -> bytes` from `vera_core.config.kms`
  - `UserIdentity` from `vera_core.models` — mutated in-place; SQLAlchemy tracks and commits
- Produces:
  - `async def enroll(kms, *, identity, account_email) -> str` — mints seed, seals it onto `identity`, returns provisioning URI
  - `async def activate(kms, *, identity, code) -> tuple[str, ...] | None` — verifies TOTP, stores recovery hashes, returns plaintext codes once
  - `async def verify(kms, *, identity, code) -> bool` — accepts TOTP or consumes a recovery code

- [ ] **Step 1: Write failing tests first**

```python
# tests/unit/auth/test_mfa.py
"""Unit tests for TOTP MFA + one-time recovery codes.

All three MFA operations (enroll, activate, verify) mutate a UserIdentity
object in-place. These tests construct the identity in-memory — no DB needed.
"""

import pyotp
import pytest
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

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
    await mfa.activate(_KMS, identity=identity, code=pyotp.TOTP(secret).now())
    assert not await mfa.verify(_KMS, identity=identity, code="000000")


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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/unit/auth/test_mfa.py -v
```

Expected: `TypeError` or `ImportError` — old `mfa.enroll` does not accept a `KeyManagementService`.

- [ ] **Step 3: Rewrite `mfa.py`**

Replace the entire content of `apps/control_plane/src/control_plane/auth/mfa.py`:

```python
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


async def enroll(
    kms: KeyManagementService, *, identity: UserIdentity, account_email: str
) -> str:
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


async def verify(
    kms: KeyManagementService, *, identity: UserIdentity, code: str
) -> bool:
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
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
python -m pytest tests/unit/auth/test_mfa.py -v
```

Expected: all tests `PASSED`.

- [ ] **Step 5: Run `just check`**

```bash
just check
```

Expected: lint + mypy PASS. mypy will report errors in `auth.py` about `mfa.enroll(secret_provider, ...)` — those are fixed in Task 4.

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/auth/mfa.py \
        tests/unit/auth/test_mfa.py
git commit -m "feat(mfa): rewrite mfa.py — envelope encryption via KMS, UserIdentity-based, async"
```

---

## Task 4: Wire KMS into App + Update Endpoints

**Files:**
- Modify: `apps/control_plane/src/control_plane/main.py`
- Modify: `apps/control_plane/src/control_plane/deps.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/auth.py`
- Modify: `apps/control_plane/pyproject.toml` (add `google-cloud-kms`)
- Modify: `tests/integration/control_plane/test_login_flow.py`

**Interfaces:**
- Consumes: `KeyManagementService`, `build_kms` from `vera_core.config.kms` (Task 1); `mfa.enroll/activate/verify` new signatures (Task 3)
- Produces: `app.state.kms: KeyManagementService`; `get_kms(request) -> KeyManagementService` dep

- [ ] **Step 1: Update `main.py`**

In `apps/control_plane/src/control_plane/main.py`:

1. Change the import line from `vera_core.config`:

```python
# Before
from vera_core.config import InMemorySecretProvider, Settings, WritableSecretProvider, get_settings
# After
from vera_core.config import Settings, get_settings
from vera_core.config.kms import KeyManagementService, build_kms
```

2. In `create_app`, replace the `secret_provider` parameter with `kms`:

```python
def create_app(
    settings: Settings | None = None,
    *,
    token_verifier: TokenVerifier | None = None,
    audit: AuditSink | None = None,
    auth_audit: AuthAuditSink | None = None,
    permission_cache: PermissionCache | None = None,
    session_store: SessionStore | None = None,
    kms: KeyManagementService | None = None,
    idempotency: IdempotencyStore | None = None,
    email_sender: EmailSender | None = None,
    invitation_store: InvitationStore | None = None,
) -> FastAPI:
```

3. In the `lifespan` function, replace:

```python
        app.state.secret_provider = secret_provider or InMemorySecretProvider()
```

With:

```python
        app.state.kms = kms or build_kms(settings)
```

- [ ] **Step 2: Update `deps.py`**

In `apps/control_plane/src/control_plane/deps.py`:

1. Change the import from `vera_core.config`:

```python
# Before
from vera_core.config import Settings, WritableSecretProvider
# After
from vera_core.config import Settings
from vera_core.config.kms import KeyManagementService
```

2. Replace `get_secret_provider` with `get_kms`:

```python
# Remove this:
def get_secret_provider(request: Request) -> WritableSecretProvider:
    provider: WritableSecretProvider = request.app.state.secret_provider
    return provider

# Add this:
def get_kms(request: Request) -> KeyManagementService:
    kms: KeyManagementService = request.app.state.kms
    return kms
```

- [ ] **Step 3: Update `auth.py` endpoints**

In `apps/control_plane/src/control_plane/api/v1/auth.py`, make the following changes:

**3a. Replace imports at the top:**

Remove:
```python
from vera_core.config import SecretNotFoundError, WritableSecretProvider
```

Add:
```python
from vera_core.config.kms import KeyManagementService
```

**3b. Replace the `get_secret_provider` import and `Secrets` alias:**

Remove:
```python
from control_plane.deps import (
    client_ip,
    current_identity,
    get_secret_provider,
    get_session_store,
    get_sessionmaker,
)
```

Replace with:
```python
from control_plane.deps import (
    client_ip,
    current_identity,
    get_kms,
    get_session_store,
    get_sessionmaker,
)
```

Remove:
```python
Secrets = Annotated[WritableSecretProvider, Depends(get_secret_provider)]
```

Add:
```python
KMS = Annotated[KeyManagementService, Depends(get_kms)]
```

**3c. Rewrite `mfa_verify` endpoint:**

```python
@router.post(
    "/tenants/{tenant_slug}/auth/mfa/verify",
    response_model=ResponseModel[SessionResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def mfa_verify(
    tenant_slug: str,
    body: MfaVerifyRequest,
    request: Request,
    sessionmaker: Sessionmaker,
    store: Store,
    kms: KMS,
    audit: AuthAudit,
    settings: AppSettings,
) -> ResponseModel[SessionResponse]:
    ip = client_ip(request)
    tenant_id = await resolve_tenant_id(sessionmaker, tenant_slug)
    challenge = await store.get(MFA_NS, body.challenge_token)
    if tenant_id is None or challenge is None or challenge.tenant_id != tenant_id:
        raise _unauthorized()

    verified = False
    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, challenge.user_id)
        if ident is not None:
            verified = await mfa.verify(kms, identity=ident, code=body.code)

    if not verified:
        await _audit(
            audit,
            tenant_id=tenant_id,
            event=AuthEvent.LOGIN_FAILURE,
            ip=ip,
            user_id=challenge.user_id,
        )
        raise _unauthorized()

    await store.delete(MFA_NS, body.challenge_token)
    token = await store.mint_session(
        replace(challenge, mfa_passed=True),
        settings.session_ttl_seconds,
        settings.session_absolute_max_seconds,
    )
    await _stamp_last_login(sessionmaker, tenant_id, challenge.user_id)
    await _audit(
        audit,
        tenant_id=tenant_id,
        event=AuthEvent.LOGIN_SUCCESS,
        ip=ip,
        user_id=challenge.user_id,
    )
    return ok(SessionResponse(session_token=token))
```

**3d. Rewrite `mfa_enroll` endpoint:**

```python
@router.post(
    "/tenants/{tenant_slug}/auth/mfa/enroll",
    response_model=ResponseModel[EnrollResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def mfa_enroll(
    tenant_id: Annotated[UUID, Depends(tenant_guard)],
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Sessionmaker,
    kms: KMS,
) -> ResponseModel[EnrollResponse]:
    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, identity.user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        uri = await mfa.enroll(kms, identity=ident, account_email=identity.email)
    return ok(EnrollResponse(provisioning_uri=uri))
```

**3e. Rewrite `mfa_activate` endpoint:**

```python
@router.post(
    "/tenants/{tenant_slug}/auth/mfa/activate",
    response_model=ResponseModel[RecoveryCodesResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def mfa_activate(
    tenant_id: Annotated[UUID, Depends(tenant_guard)],
    body: MfaActivateRequest,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Sessionmaker,
    kms: KMS,
) -> ResponseModel[RecoveryCodesResponse]:
    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, identity.user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        codes = await mfa.activate(kms, identity=ident, code=body.code)
        if codes is None:
            raise BadRequestError(message="invalid code")
        ident.mfa_enabled = True
    return ok(RecoveryCodesResponse(recovery_codes=list(codes)))
```

**3f. Rewrite the MFA section in `accept_invitation`:**

In `accept_invitation`, replace the `secret_provider: Secrets` parameter with `kms: KMS`, and update the MFA section:

Replace the parameter declaration:
```python
    secret_provider: Secrets,
```
With:
```python
    kms: KMS,
```

Replace the MFA block inside `async with tenant_session(...)`:
```python
        if enforce_mfa:
            secret_ref = mfa.mfa_secret_ref(tenant_id, user.id)
            identity.mfa_secret_ref = secret_ref
            provisioning_uri = mfa.enroll(
                secret_provider, secret_ref=secret_ref, account_email=invite.email
            )
```
With:
```python
        if enforce_mfa:
            provisioning_uri = await mfa.enroll(kms, identity=identity, account_email=invite.email)
```

**3g. Rewrite `activate_invitation_mfa`:**

Replace parameter `secret_provider: Secrets` with `kms: KMS`.

Replace:
```python
    secret_ref = mfa.mfa_secret_ref(tenant_id, invite.app_user_id)
    try:
        codes = mfa.activate(secret_provider, secret_ref=secret_ref, code=body.code)
    except SecretNotFoundError:
        codes = None
    if codes is None:
        raise BadRequestError(message="invalid code")

    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, invite.app_user_id)
        if ident is not None:
            ident.mfa_enabled = True
        await session.execute(
            update(AppUser).where(AppUser.id == invite.app_user_id).values(status="active")
        )
```

With:
```python
    async with tenant_session(sessionmaker, tenant_id) as session:
        ident = await _password_identity_row(session, invite.app_user_id)
        if ident is None:
            raise BadRequestError(message="no password identity for user")
        codes = await mfa.activate(kms, identity=ident, code=body.code)
        if codes is None:
            raise BadRequestError(message="invalid code")
        ident.mfa_enabled = True
        await session.execute(
            update(AppUser).where(AppUser.id == invite.app_user_id).values(status="active")
        )
```

Also remove the dead import of `mfa.mfa_secret_ref` — the `mfa_secret_ref` function is gone.

- [ ] **Step 4: Add `google-cloud-kms` to `control_plane/pyproject.toml`**

In `apps/control_plane/pyproject.toml`, add to `dependencies`:

```toml
"google-cloud-kms>=2.21",
```

This is a prod dep. The lazy `from google.cloud import kms` import in `GCPCloudKMS` means it is only resolved at runtime when `GCPCloudKMS` is instantiated (i.e., when `kms_key_name` is set). Local dev and CI never hit it.

- [ ] **Step 5: Update integration test fixture**

In `tests/integration/control_plane/test_login_flow.py`:

Replace the import:
```python
from vera_core.config import InMemorySecretProvider, Settings
```
With:
```python
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
```

In the `login_world` fixture, replace:
```python
    app = create_app(
        settings,
        session_store=InMemorySessionStore(),
        secret_provider=InMemorySecretProvider(),
        permission_cache=InMemoryPermissionCache(),
    )
```
With:
```python
    app = create_app(
        settings,
        session_store=InMemorySessionStore(),
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
    )
```

- [ ] **Step 6: Run `just check`**

```bash
just check
```

Expected: all lint, type, and tests PASS. The integration test `test_mfa_enroll_activate_then_challenge_flow` exercises the full MFA path end-to-end against a live DB.

- [ ] **Step 7: Commit**

```bash
git add apps/control_plane/src/control_plane/main.py \
        apps/control_plane/src/control_plane/deps.py \
        apps/control_plane/src/control_plane/api/v1/auth.py \
        apps/control_plane/pyproject.toml \
        tests/integration/control_plane/test_login_flow.py
git commit -m "feat(control-plane): wire KMS into create_app, update MFA endpoints to use DB envelope encryption"
```

---

## Task 5: Remove Dead Code

Now that all callers are migrated, remove the `WritableSecretProvider`/`InMemorySecretProvider`/`GoogleSecretManagerProvider` cluster which was exclusively used for the old MFA secret-store pattern.

**Files:**
- Modify: `packages/vera_core/src/vera_core/config/secrets.py`
- Modify: `packages/vera_core/src/vera_core/config/__init__.py`

- [ ] **Step 1: Trim `secrets.py`**

Remove `WritableSecretProvider`, `InMemorySecretProvider`, and `GoogleSecretManagerProvider` from `packages/vera_core/src/vera_core/config/secrets.py`. Keep `SecretNotFoundError`, `SecretProvider`, and `EnvSecretProvider` — these remain useful for resolving platform/env config secrets.

The file should read:

```python
"""Secret resolution boundary.

Application code asks a SecretProvider for a named secret and never reads
credential env vars directly. Local dev uses EnvSecretProvider; production uses
Google Secret Manager (CMEK-encrypted) behind the same interface.
"""

import os
from typing import Protocol, runtime_checkable


class SecretNotFoundError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"secret not found: {name}")
        self.name = name


@runtime_checkable
class SecretProvider(Protocol):
    def get(self, name: str) -> str:
        """Return the secret value for `name`; raise SecretNotFoundError if absent."""
        ...


class EnvSecretProvider:
    """Local dev: secrets come from environment variables (optionally prefixed)."""

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix

    def get(self, name: str) -> str:
        key = f"{self._prefix}{name}".upper().replace("-", "_")
        value = os.environ.get(key)
        if value is None:
            raise SecretNotFoundError(name)
        return value
```

- [ ] **Step 2: Update `__init__.py`**

Replace the content of `packages/vera_core/src/vera_core/config/__init__.py` with:

```python
from .kms import KeyManagementService, LocalDevKMS, GCPCloudKMS, build_kms, seal, open_sealed
from .secrets import EnvSecretProvider, SecretNotFoundError, SecretProvider
from .settings import Settings, get_settings

__all__ = [
    "EnvSecretProvider",
    "GCPCloudKMS",
    "KeyManagementService",
    "LocalDevKMS",
    "SecretNotFoundError",
    "SecretProvider",
    "Settings",
    "build_kms",
    "get_settings",
    "open_sealed",
    "seal",
]
```

- [ ] **Step 3: Run `just check`**

```bash
just check
```

Expected: PASS. If any file still imports `WritableSecretProvider` or `InMemorySecretProvider`, mypy will catch it here.

- [ ] **Step 4: Commit**

```bash
git add packages/vera_core/src/vera_core/config/secrets.py \
        packages/vera_core/src/vera_core/config/__init__.py
git commit -m "chore(vera-core): remove WritableSecretProvider, InMemorySecretProvider, GoogleSecretManagerProvider — replaced by KMS"
```

---

## Task 6: Docs + Ops

**Files:**
- Modify: `adr/devops-todo.md`
- Modify: `CLAUDE.md` (repo root)
- Modify: `packages/vera_core/src/vera_core/CLAUDE.md`
- Modify: `apps/control_plane/src/control_plane/CLAUDE.md`

- [ ] **Step 1: Update `devops-todo.md`**

Add a new row to the table in `adr/devops-todo.md`:

```markdown
| 2 | ☐ **Provision a Cloud KMS key ring and symmetric encryption key for MFA envelope encryption**, grant the GKE workload-identity service account `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the specific key, and set `VERA_KMS_KEY_NAME` (full resource path: `projects/{project}/locations/{location}/keyRings/{ring}/cryptoKeys/{key}`) in the GKE deployment env. Without this, `build_kms` falls back to `LocalDevKMS` and startup will fail with a `ValueError` (`LOCAL_KMS_MASTER_KEY` not set). | MFA TOTP seeds are envelope-encrypted at rest: the DEK is wrapped by Cloud KMS (`GCPCloudKMS`), the ciphertext lives in `user_identity.totp_seed_ct`. Key rotation is forward-safe: `totp_key_ref` stores the version that wrapped each row's DEK; Cloud KMS `decrypt` selects the correct version automatically. | MFA DB envelope encryption (2026-06-19). |
```

- [ ] **Step 2: Update root `CLAUDE.md`**

In the `Build, test & layout` section of the root `CLAUDE.md`, add a bullet after the `just up` / `just migrate` line:

```markdown
- `LOCAL_KMS_MASTER_KEY` — required for local dev when `VERA_KMS_KEY_NAME` is unset.
  Generate once: `python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"`.
  In production, set `VERA_KMS_KEY_NAME` to the Cloud KMS key resource path instead (see `adr/devops-todo.md`).
```

- [ ] **Step 3: Update `vera_core/CLAUDE.md`**

After the `## PHI at rest` paragraph, add a new section:

```markdown
## Envelope encryption (`vera_core.config.kms`)

TOTP seeds are envelope-encrypted: AES-256-GCM under a per-user DEK, DEK wrapped by a
`KeyManagementService`. The DEK is ephemeral — never persisted. Use `seal(kms, plaintext)` /
`open_sealed(kms, ...)` from `vera_core.config.kms` for any new credential that needs the
same pattern. Never store the plaintext DEK or the plaintext seed anywhere outside the call stack.

In dev, `LocalDevKMS` wraps DEKs with AES-256-GCM under `LOCAL_KMS_MASTER_KEY` (env var).
In prod, `GCPCloudKMS` delegates to Cloud KMS (Workload Identity, see `adr/devops-todo.md` #2).
`build_kms(settings)` selects the implementation: set `VERA_KMS_KEY_NAME` → GCP; unset → local.
```

- [ ] **Step 4: Update `control_plane/CLAUDE.md`**

Add a short section after the `## Minimum necessary` section:

```markdown
## KMS dep injection

`app.state.kms` holds the process-wide `KeyManagementService`. `build_kms(settings)` picks the
implementation from `settings.kms_key_name`. Tests always inject `LocalDevKMS(master_key=b"a"*32)`
directly into `create_app(kms=...)` — never rely on the env var in tests. Never construct a KMS
instance outside of `build_kms` or test fixtures.
```

- [ ] **Step 5: Run `just check`**

```bash
just check
```

Expected: PASS (docs-only changes, but run to be safe).

- [ ] **Step 6: Commit**

```bash
git add adr/devops-todo.md \
        CLAUDE.md \
        packages/vera_core/src/vera_core/CLAUDE.md \
        apps/control_plane/src/control_plane/CLAUDE.md
git commit -m "docs(ops): add GCP KMS infra obligation, update CLAUDE.md files for envelope-encryption pattern"
```

---

## Post-Implementation

After all 6 tasks are committed, run the `/simplify` skill on the full diff since the start of this branch. Then run `just check` one final time to confirm the simplification pass did not break anything.

---

## Self-Review

**Spec coverage check:**

| Requirement | Task |
|---|---|
| TOTP seed envelope-encrypted in DB | Tasks 2, 3 |
| DEK wrapped by `KeyManagementService` | Tasks 1, 3 |
| Recovery code bcrypt hashes in JSONB on `user_identity` | Tasks 2, 3 |
| `LocalDevKMS` with `LOCAL_KMS_MASTER_KEY` env var | Task 1 |
| `GCPCloudKMS` for production | Task 1 |
| `build_kms` factory switches on `settings.kms_key_name` | Task 1 |
| `kms_key_name` in `Settings` | Task 1 |
| `create_app(kms=...)` injectable | Task 4 |
| `mfa.py` async, `UserIdentity`-based, no `WritableSecretProvider` | Task 3 |
| All MFA HTTP endpoints use KMS dep | Task 4 |
| Dead code removed | Task 5 |
| Migration `0009` | Task 2 |
| `adr/devops-todo.md` row for Cloud KMS | Task 6 |
| Three `CLAUDE.md` files updated | Task 6 |
| `google-cloud-kms` dep in `control_plane` | Task 4 |
| `cryptography` dep in `vera_core` | Task 1 |
| Integration test passes end-to-end | Task 4 |
| `just check` clean throughout | all tasks |

**Placeholder scan:** No TBDs, no "implement later", all code blocks are complete.

**Type consistency:**
- `mfa.enroll(kms, *, identity, account_email)` — used exactly this way in Tasks 3 and 4.
- `mfa.activate(kms, *, identity, code)` — same.
- `mfa.verify(kms, *, identity, code)` — same.
- `seal(kms, plaintext) -> (seed_ct, dek_ct, key_ref)` — used exactly in `mfa.enroll`.
- `open_sealed(kms, seed_ct, dek_ct, key_ref)` — used exactly in `mfa._decrypt_seed`.
- `LocalDevKMS(master_key=b"a"*32)` — used in tests in Tasks 3 and 4.
