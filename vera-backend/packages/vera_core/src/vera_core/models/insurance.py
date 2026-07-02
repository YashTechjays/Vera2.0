"""Insurance master data — GLOBAL (no tenant_id, no RLS, no PHI).

A provider's working-hours gate and its IVR playbook are reference data shared
across tenants. The real FK `ivr_playbook.provider_id -> insurance_provider`
fixes v1's `prompts.insurance_provider_name` string-match anti-pattern (ADR §1).
"""

from datetime import time
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, Time, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, TimestampMixin, UUIDv7PKMixin


class InsuranceProvider(Base, UUIDv7PKMixin, TimestampMixin):
    __tablename__ = "insurance_provider"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    working_hour_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    working_hour_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class IvrPlaybook(Base, UUIDv7PKMixin, TimestampMixin):
    __tablename__ = "ivr_playbook"

    # At most one active playbook per provider — runtime selection resolves exactly one
    # (mirrors the published-per-family partial unique index on authoring.PromptVersion).
    __table_args__ = (
        Index(
            "uq_ivr_playbook_active_per_provider",
            "provider_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    provider_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("insurance_provider.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    instructions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
