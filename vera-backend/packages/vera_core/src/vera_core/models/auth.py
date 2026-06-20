"""Authentication / authorization model layered under GCIP + RBAC (ADR §3.5).

GCIP (the BAA-covered Firebase Auth replacement) verifies the upstream identity;
these tables hold the app-side mapping, the local `password` provider, the
SUPER_ADMIN scoped-elevation trail, and the authN/Z audit log.

Credentials here (`hashed_password`, `totp_seed_ct`, `totp_dek_ct`) are sensitive
but are NOT PHI — no PHI_INFO markers. TOTP material is stored as envelope-encrypted
ciphertext in the DB (see ADR for `mfa-db-envelope-encryption`).

RLS note: `sso_provider`, `user_identity`, and `tenant_elevation` carry a
`tenant_id` (denormalized where needed) and join the standard tenant-isolation
policy. `auth_audit_log` is WORM (append+select only, like `audit_log`) and its
`tenant_id` is NULLABLE for platform-level events (e.g. SUPER_ADMIN login); those
platform rows are written/read out of band, tenant sessions only ever see their
own (ADR §3.5.7).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import (
    Base,
    CreatedAtMixin,
    NullableTenantColumnMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDv7PKMixin,
)
from vera_core.models.enums import AuthEvent, ProviderKind, check_in


class SsoProvider(Base, TenantScopedMixin):
    """Per-tenant IdP config keyed to a GCIP provider. `enabled` is the tenant
    on/off toggle (TENANT_ADMIN, perm `tenant:auth:configure`); `enforce_mfa`
    encodes the "password login requires 2FA" rule as data (ADR §3.5.8)."""

    __tablename__ = "sso_provider"
    __table_args__ = (check_in("provider_type", ProviderKind),)

    gcip_provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enforce_mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PlatformLoginProvider(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
    """The single global platform-operator login provider — the tenant-less
    analogue of `sso_provider` (ADR-0006 §D). One row, seeded `password`, gates
    how SUPER_ADMIN / `account_type='platform'` operators authenticate. `enabled`
    is the platform on/off toggle; `enforce_mfa` encodes the "platform login
    requires 2FA" rule as data. A GCIP SSO provider can drop in behind the same
    seam later. `tenant_id IS NULL` (platform-readable RLS); the UNIQUE on
    `provider_type` keeps it effectively single-row per kind."""

    __tablename__ = "platform_login_provider"
    __table_args__ = (
        UniqueConstraint("provider_type", name="uq_platform_login_provider_provider_type"),
        check_in("provider_type", ProviderKind),
    )

    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enforce_mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserIdentity(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
    """A federated (or local-password) identity linked to one `app_user`.
    `tenant_id` is denormalized from the app_user so RLS stays a plain column
    compare. The local `password` provider stores its bcrypt hash + MFA here, so
    identity secrets stay off `user_account` and out of the GCIP path.

    `tenant_id` is NULLABLE: a platform operator (`account_type='platform'`) has
    no tenant, so its password identity row carries `tenant_id IS NULL` and is
    reached only via the platform-readable RLS policy (ADR-0006 §D)."""

    __tablename__ = "user_identity"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_subject"),
        check_in("provider_type", ProviderKind),
    )

    app_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    gcip_provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # password provider only:
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    totp_seed_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_dek_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    totp_key_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    recovery_code_hashes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)


class TenantElevation(Base, UUIDv7PKMixin, TimestampMixin):
    """A SUPER_ADMIN's time-boxed, single-tenant elevation grant (ADR §3.5.4).

    **Platform-governance table — NOT tenant-scoped** (ADR §3.5.9). It carries
    `target_tenant_id` (the tenant elevated into), not a generic `tenant_id`, and
    gets a bespoke RLS policy in the migration: an elevated session (whose GUC =
    the target) reads its own grant; the platform "all active elevations" oversight
    read + grant creation are the deferred platform runtime. `super_admin_user_id`
    is the real operator; PHI rows read while elevated link back here via
    `audit_log.elevation_session_id`, answering "which human, and why in this
    tenant". A `reason` is required (break-glass); `expires_at` bounds it.

    A partial unique index enforces AT MOST ONE active (un-ended) grant per
    operator — break-glass is deliberate and singular. A plain UNIQUE on
    (super_admin_user_id, ended_at) would NOT: Postgres treats NULL ended_at as
    distinct, so two active grants would slip through.
    """

    __tablename__ = "tenant_elevation"
    __table_args__ = (
        Index(
            "uq_tenant_elevation_active",
            "super_admin_user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    super_admin_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthAuditLog(Base, UUIDv7PKMixin, CreatedAtMixin):
    """WORM authN/Z audit trail with a per-row hash chain — separate from the PHI
    `audit_log` but the same immutability discipline. `tenant_id` is nullable for
    platform-level events; the migration gives it SELECT/INSERT-only policies."""

    __tablename__ = "auth_audit_log"
    __table_args__ = (check_in("event_type", AuthEvent),)

    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    app_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    gcip_provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    row_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
