from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, TimestampMixin, UUIDv7PKMixin


class Tenant(Base, UUIDv7PKMixin, TimestampMixin):
    """A customer organization — the multi-tenant root.

    RLS on this table keys on `id` (not tenant_id): a request sees only its own
    tenant row. Holds the runtime knobs the worker reads per call
    (max_agents_per_va, max_concurrent_calls, retry_fill_threshold, auto_retry_enabled,
    persona_tweak) so behaviour is tenant config, not code.

    `gcip_tenant_id` maps this row to its Google Cloud Identity Platform tenant
    (GCIP is natively multi-tenant); nullable so a tenant can exist before its
    GCIP tenant is provisioned.

    `slug` is the URL-facing tenant identifier (e.g. `/tenants/{slug}/auth/login`):
    a human-readable, globally-unique handle the user supplies at login since nobody
    can recall the UUID. Lowercase DNS-label style (`[a-z0-9-]`, <=63 chars), and
    treated as immutable once set — changing it breaks existing tenant URLs.
    """

    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    gcip_tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Runtime knobs (spec Fig 7). retry_fill_threshold is the fill-% below which
    # a form auto-requeues for a retry call; persona_tweak overlays the prompt.
    max_agents_per_va: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # Tenant-wide dial ceiling (dispatcher slot math). Distinct from the per-VA
    # in-flight cap above, which gates each VA at enqueue time.
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    retry_fill_threshold: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0.50)
    # Per-tenant auto-retry switch, ANDed with the deployment kill-switch
    # (VERA_FORM_AUTO_RETRY_ENABLED); platform-managed, off until enabled.
    auto_retry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    persona_tweak: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    queue_expiry_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48)
    observer_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Recording retention in days; NULL → the platform default
    # (settings.recording_retention_days_default). Stamped onto
    # recording.retention_until at verify time; changing it does NOT rewrite
    # already-stamped recordings (spec decision).
    recording_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        # Mirrors the API bound (RetentionPolicyUpdate 1..3650): a direct DB write
        # of 0/negative days would silently corrupt retention_until stamping.
        # NULL passes a CHECK per SQL semantics, so no explicit IS NULL arm
        # (same idiom as oversight.score_range / field_answer.confidence_range).
        CheckConstraint(
            "recording_retention_days BETWEEN 1 AND 3650",
            name="recording_retention_days_range",
        ),
    )

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
