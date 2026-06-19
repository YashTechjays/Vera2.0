from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, NullableTenantColumnMixin, TimestampMixin, UUIDv7PKMixin
from vera_core.models.enums import AccountType


class AppUser(Base, UUIDv7PKMixin, TimestampMixin, NullableTenantColumnMixin):
    """A human operator (ADR §3.5). The agent worker is NOT an app_user — it
    authenticates as a GCP service principal and never goes through RBAC.

    Table is named `app_user` (canonical, ADR §3.5.9) — and it dodges the SQL
    reserved word `user`.

    Two account types (ADR §3.5.9):
    - `tenant`   — a tenant member; `tenant_id` IS NOT NULL.
    - `platform` — a platform operator (SUPER_ADMIN); `tenant_id` IS NULL, with no
      standing PHI access (privilege comes only from an RBAC role + scoped elevation).
    A DB CHECK pairs `account_type` with `tenant_id` nullability so the invariant is
    enforced by the database, not just application code. `account_type` confers no
    power by itself — it only governs scoping/home.

    `gcip_uid` is the GCIP user UID (the verified token's `sub`), globally unique;
    NULLABLE so a local-`password`-provider-only operator can exist without a GCIP
    identity (the unique index treats NULLs as distinct).
    """

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("account_type IN ('tenant','platform')", name="account_type_valid"),
        CheckConstraint(
            "(account_type = 'platform' AND tenant_id IS NULL)"
            " OR (account_type = 'tenant' AND tenant_id IS NOT NULL)",
            name="tenant_binding",
        ),
        Index("ix_app_user_tenant_status", "tenant_id", "status"),
    )

    gcip_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    account_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AccountType.TENANT.value
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
