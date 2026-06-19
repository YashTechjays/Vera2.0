"""RBAC tables (ADR §3.5).

`permission` is a global catalog keyed by `code` (e.g. "calls:read"), no tenant_id.
`role` is the shared-catalog + tenant-extension pattern: the seeded system/template
roles (`SUPER_ADMIN`, `TENANT_ADMIN`, `SUPERVISOR`) are global (`tenant_id IS NULL`)
and shared across tenants — a tenant assigns them to its users via `user_role`
without per-tenant copies — while a tenant adds its own custom roles (`tenant_id`
set). `SUPER_ADMIN` is global but platform-tier: it carries `platform:*` permissions
and is therefore never tenant-assignable (enforced in `api/v1/roles.py`). Effective
permissions are resolved server-side from these tables — never from token claims.

RLS: `role` and `role_permission` carry the *catalog* policy (a tenant session may
READ global rows + its own, but only WRITE its own); `user_role` carries the strict
tenant policy. See migration 0001 and `db.rls.catalog_rls_policy_ddl`.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import (
    Base,
    NullableTenantColumnMixin,
    TimestampMixin,
    UUIDv7PKMixin,
)


class Permission(Base, UUIDv7PKMixin, TimestampMixin):
    """Global permission catalog — not tenant-scoped, no RLS (no PHI, read-only)."""

    __tablename__ = "permission"

    code: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Role(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
    """A role, identified by `name`. `tenant_id IS NULL` ⇒ a global system/template
    role shared across tenants (`SUPER_ADMIN`/`TENANT_ADMIN`/`SUPERVISOR`); otherwise
    a tenant's custom role. NULLS NOT DISTINCT so a global role name can't be
    duplicated."""

    __tablename__ = "role"
    __table_args__ = (UniqueConstraint("tenant_id", "name", postgresql_nulls_not_distinct=True),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RolePermission(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
    """Join table. `tenant_id` is denormalized from the role (NULL for a global
    role's grants) so the RLS policy stays a plain column comparison."""

    __tablename__ = "role_permission"
    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)

    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("role.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("permission.id", ondelete="CASCADE"), nullable=False
    )


class UserRole(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
    """A role assignment to an app_user. `tenant_id` is the tenant the grant applies
    in (NULL only for a platform user's global-role grant). Strict RLS — a tenant
    session sees only its own assignments."""

    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("app_user_id", "role_id"),)

    app_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("role.id", ondelete="CASCADE"), nullable=False
    )
    granted_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
