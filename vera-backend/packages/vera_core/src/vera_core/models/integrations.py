"""Credentials, split by direction (ADR §2.1) — the call I made over the spec's
flat `tenant_credential`:

- `api_key`  — **inbound** keys Vera *issues* and only ever **verifies** ⇒ store a
  salted hash (irreversible) + scope + expiry + revoke. Tenant-scoped.
- `integration` — **outbound** secrets Vera *presents* to a third party (Twilio,
  EMR) and must **recover** ⇒ the credential is **envelope-encrypted in the DB**
  (same scheme as the MFA seed, `vera_core.config.kms`): `credential_ct` holds the
  AES-256-GCM ciphertext of the credential JSON, `dek_ct` the KMS-wrapped DEK, and
  `secret_ref` the KMS key-version reference (rotation/audit) + `rotated_at`.
  Tenant-owned, one per type per tenant; RLS keeps it private to its tenant.
- `integration_type` — the GLOBAL catalog (no tenant_id); `credentials_schema`
  drives validation + the dynamic UI form + test-before-save (kept from v1).

These are credentials, not PHI; no PHI_INFO markers here.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDv7PKMixin


class IntegrationType(Base, UUIDv7PKMixin, TimestampMixin):
    """Global catalog of integration kinds (Twilio, EMR, …)."""

    __tablename__ = "integration_type"

    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    credentials_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class Integration(Base, TenantScopedMixin):
    """A tenant's outbound credential for one integration type, envelope-encrypted
    in place (see `vera_core.integrations.credentials`). The plaintext credential
    never lands in the DB — only the ciphertext + wrapped DEK + key reference."""

    __tablename__ = "integration"
    __table_args__ = (UniqueConstraint("tenant_id", "integration_type_id"),)

    integration_type_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("integration_type.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Envelope encryption (mirrors user_identity's MFA columns):
    #   credential_ct — AES-256-GCM ciphertext of the credential JSON
    #   dek_ct        — KMS-wrapped Data Encryption Key (per row)
    #   secret_ref    — KMS key-version reference (rotation/audit)
    credential_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dek_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiKey(Base, TenantScopedMixin):
    """An inbound key Vera issued to an external system. Hash only — never the
    value — so a DB leak cannot replay it.

    The token presented by the caller is `vk_<tenant_id>.<key_id>.<secret>`; we store
    a per-key random `salt` and `key_hash = sha256(salt || secret)`. Verification
    looks the row up by `id` (embedded in the token) and recomputes the hash — the
    raw secret is shown once at issuance and never persisted (auth/api_key.py)."""

    __tablename__ = "api_key"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    salt: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scope: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
